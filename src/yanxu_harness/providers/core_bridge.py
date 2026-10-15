"""The provider → core bridge: one adapter, no second model loop.

Core asks a provider for an *assistant message stream*: frames carrying a growing
``partial`` message, plus one ``result()``. The provider port speaks a different
vocabulary: neutral :class:`~yanxu_harness.providers.protocol.ProviderEvent`
frames assembled by a vendor adapter. This module is the only place the two meet;
everything else in the package either speaks provider or speaks core, never both.

Four rules the adapter enforces rather than documents:

* **The deltas and the final message cannot disagree.** Frames are snapshots of
  one accumulator, and ``result()`` returns the same accumulator's final state,
  so a host that only reads deltas and a host that only reads the result see the
  same answer (the provider guarantees the same property about its own frames).
* **A failure is a terminal frame, not an exception.** Core's ``stream_fn``
  contract says a model failure must be encoded as ``stop_reason=error/aborted``
  with ``error_message``. Raising out of the loop would lose the partial answer
  and the machine-readable kind; widening the exception would fabricate a
  finished turn.
* **Nothing is invented.** A stream without a terminal frame, an unrecognised
  finish reason, and a missing usage field are all reported as what they are:
  ``error``, ``error``, and "unknown". Never ``stop``, never ``0``.
* **Private continuation material stays available but is not published.** A
  thinking signature lands on the message block (where re-sending a turn can use
  it) and never in a frame's own fields; the public projection of a core event
  structurally drops the raw provider frame
  (``yanxu_harness.core.events.public_event_payload``).

The adapter adds no retries: one provider call per model turn, and a failed turn
ends the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Mapping

from ..core.messages import (
    content_text,
    get_current_system_prompt,
    get_current_tools,
    render_system_message_update,
    resolve_transcript,
    without_initial_system_message,
)
from ..core.types import (
    AssistantMessage,
    Model,
    StopReason,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolDeclaration,
    TranscriptContext,
    Usage,
)
from ..errors import ContractError, ProviderFailure
from .protocol import (
    Provider,
    ProviderEvent,
    ProviderEventKind,
    ProviderFailureKind,
    ProviderRequest,
    ProviderToolSpec,
)

__all__ = [
    "CoreStreamBridge",
    "ProviderCoreStream",
    "STOP_REASON_MAP",
    "core_usage",
    "map_stop_reason",
    "to_provider_request",
    "wire_message",
]

#: ``yanxu_harness.core.agent.DEFAULT_MODEL``: the sentinel core uses when a host
#: did not configure a model. It is not a model name and must never reach a wire
#: request, so the provider config's model wins over it.
UNKNOWN_MODEL_ID = "unknown"

#: Vendor finish reason → core stop reason. Anything not listed here is reported
#: as ``error`` with the raw reason in ``raw_stop_reason``: a reason this build
#: does not understand is not a finished answer.
STOP_REASON_MAP: Mapping[str, StopReason] = {
    "stop": StopReason.STOP,
    "end_turn": StopReason.STOP,
    "stop_sequence": StopReason.STOP,
    "tool_calls": StopReason.TOOL_USE,
    "tool_use": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.LENGTH,
    "max_tokens": StopReason.LENGTH,
    "deferred": StopReason.DEFERRED,
    "aborted": StopReason.ABORTED,
    "cancelled": StopReason.ABORTED,
    "error": StopReason.ERROR,
}

#: Provider failure kind → core stop reason. Cancellation is ``aborted`` (the
#: caller asked for it); everything else is ``error``.
FAILURE_STOP_REASON: Mapping[ProviderFailureKind, StopReason] = {
    ProviderFailureKind.CANCELLED: StopReason.ABORTED,
}

_STATUS_KEYS = ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens", "reasoning_tokens")


def map_stop_reason(raw: str) -> StopReason:
    """Map one vendor finish reason; unknown reasons are ``error``, never ``stop``."""
    return STOP_REASON_MAP.get((raw or "").strip().lower(), StopReason.ERROR)


def _optional_int(value: Any) -> int | None:
    """Keep "not reported" as ``None``. A non-numeric value degrades to unknown.

    Degrading is deliberate: inventing ``0`` would let a host bill an unmeasured
    request as free, and raising over a broken counter would throw away a partial
    answer that the caller already saw on the wire.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def core_usage(usage: Mapping[str, Any]) -> Usage:
    """Provider usage map → core :class:`Usage`, with missing fields left missing."""
    if not usage:
        return Usage()
    return Usage(
        input=_optional_int(usage.get("prompt_tokens", usage.get("input_tokens"))),
        output=_optional_int(usage.get("completion_tokens", usage.get("output_tokens"))),
        total_tokens=_optional_int(usage.get("total_tokens")),
        reasoning=_optional_int(usage.get("reasoning_tokens")),
        cache_read=_optional_int(usage.get("cache_read_tokens", usage.get("cached_tokens"))),
        cache_write=_optional_int(usage.get("cache_write_tokens")),
    )


def wire_message(message: Any, *, send_thinking: bool = False) -> Mapping[str, Any]:
    """Convert one core message into one OpenAI-compatible wire message.

    An unsupported block type is refused explicitly: dropping it would silently
    change what the model is answering, and sending it as text would be worse.
    """
    role = getattr(message, "role", None)
    if role == "system":
        return {"role": "system", "content": render_system_message_update(message)}
    if role == "user":
        return {"role": "user", "content": _user_content(message.content)}
    if role == "assistant":
        return _assistant_content(message, send_thinking=send_thinking)
    if role == "toolResult":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": content_text(message.content),
        }
    raise ContractError(f"cannot send a {role!r} message to a chat-completions endpoint")


def _user_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    parts: list[Mapping[str, Any]] = []
    for block in content or ():
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append({"type": "text", "text": block.text})
        elif kind == "image":
            mime = block.mime_type or "application/octet-stream"
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{block.data}"}})
        else:
            raise ContractError(f"a user message cannot carry a {kind!r} block over this transport")
    return parts or ""


def _assistant_content(message: AssistantMessage, *, send_thinking: bool) -> Mapping[str, Any]:
    payload: dict[str, Any] = {"role": "assistant", "content": content_text(message.content)}
    calls = [block for block in message.content if getattr(block, "type", None) == "toolCall"]
    if calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
                },
            }
            for call in calls
        ]
    if send_thinking:
        # Opt-in re-send of private continuation material. Most OpenAI-compatible
        # endpoints reject an unexpected `reasoning_content`; the material is kept
        # in the transcript either way, so the host decides when to replay it.
        thinking = "".join(
            block.thinking for block in message.content if getattr(block, "type", None) == "thinking"
        )
        if thinking:
            payload["reasoning_content"] = thinking
        signature = next(
            (
                block.thinking_signature
                for block in message.content
                if getattr(block, "type", None) == "thinking" and block.thinking_signature
            ),
            None,
        )
        if signature:
            payload["signature"] = signature
    return payload


def provider_tool_spec(declaration: ToolDeclaration) -> ProviderToolSpec:
    """One transcript tool declaration → the wire shape of that tool.

    ``strict`` is claimed only when the deployment asked for constrained sampling.
    Defaulting to ``True`` would tell the endpoint to enforce a schema subset this
    package never validated, and a 400 there is a failure the model cannot fix.
    """
    constrained = declaration.constrained_sampling
    return ProviderToolSpec(
        name=declaration.name,
        description=declaration.description,
        parameters=dict(declaration.parameters or {"type": "object", "properties": {}}),
        strict=constrained is not None and constrained is not False,
    )


def to_provider_request(
    model: Model | None,
    context: TranscriptContext,
    options: Mapping[str, Any] | None = None,
    *,
    max_output_tokens: int | None = None,
    send_thinking: bool = False,
) -> ProviderRequest:
    """Build the wire request from the core transcript.

    Mid-conversation system messages are collapsed into the head unless the model
    declares support for them (:func:`~yanxu_harness.core.messages.resolve_transcript`),
    and the head becomes the request's ``system`` field — one place, no duplication
    between "system prompt" and "messages[0]".

    ``model`` may be ``None``: core's ``AgentLoopConfig.model`` defaults to it, and
    a host that let the provider's own config name the model is not misconfigured.
    An unknown capability is treated as unsupported, never as supported.
    """
    options = options or {}
    resolved = resolve_transcript(
        TranscriptContext(messages=tuple(context.messages)),
        bool(getattr(model, "supports_mid_convo_system_messages", False)),
    )
    system = get_current_system_prompt(resolved.messages)
    body = without_initial_system_message(resolved.messages)
    return ProviderRequest(
        messages=[wire_message(message, send_thinking=send_thinking) for message in body],
        tools=[provider_tool_spec(declaration) for declaration in get_current_tools(resolved.messages)],
        system=system,
        max_output_tokens=_output_cap(model, options, max_output_tokens),
        model=_wire_model(model, options),
        metadata={"reasoning": options.get("reasoning")} if options.get("reasoning") else {},
    )


def _wire_model(model: Model | None, options: Mapping[str, Any]) -> str:
    override = options.get("model")
    if isinstance(override, str) and override:
        return override
    model_id = getattr(model, "id", "") or ""
    return "" if model_id == UNKNOWN_MODEL_ID else model_id


def _output_cap(model: Model | None, options: Mapping[str, Any], explicit: int | None) -> int | None:
    declared = options.get("max_output_tokens")
    for candidate in (explicit, declared, getattr(model, "max_output_tokens", None)):
        if type(candidate) is int and candidate > 0:
            return candidate
    return None


class _Blocks:
    """The single accumulator behind the frames *and* the final message."""

    def __init__(self) -> None:
        self.blocks: list[Any] = []
        self.usage = Usage()
        self._text_at: int | None = None
        self._thinking_at: int | None = None

    def begin_text(self) -> None:
        self._text_at = self._index_of(TextContent)
        self._thinking_at = None

    def add_text(self, chunk: str) -> None:
        if self._text_at is None:
            self.begin_text()
        assert self._text_at is not None
        current = self.blocks[self._text_at]
        self.blocks[self._text_at] = replace(current, text=current.text + chunk)

    def begin_thinking(self) -> None:
        self._thinking_at = self._index_of(ThinkingContent)
        self._text_at = None

    def add_thinking(self, chunk: str, signature: str | None = None) -> None:
        if self._thinking_at is None:
            self.begin_thinking()
        assert self._thinking_at is not None
        current = self.blocks[self._thinking_at]
        self.blocks[self._thinking_at] = replace(
            current,
            thinking=current.thinking + chunk,
            # Continuation material travels on the block, never in a frame field.
            thinking_signature=signature or current.thinking_signature,
        )

    def begin_tool_call(self, call_id: str, name: str) -> None:
        """Open a tool-call block with no arguments yet (they arrive in one piece)."""
        self._text_at = None
        self._thinking_at = None
        self.blocks.append(ToolCall(id=call_id, name=name, arguments={}))

    def complete_tool_call(self, arguments: Mapping[str, Any]) -> None:
        """Attach the parsed arguments to the call opened by ``begin_tool_call``."""
        if not self.blocks or not isinstance(self.blocks[-1], ToolCall):
            raise ContractError("a tool call must be opened before its arguments arrive")
        self.blocks[-1] = replace(self.blocks[-1], arguments=dict(arguments))

    def _index_of(self, block_type: type) -> int:
        if self.blocks and isinstance(self.blocks[-1], block_type):
            return len(self.blocks) - 1
        self.blocks.append(block_type())
        return len(self.blocks) - 1

    def message(
        self,
        *,
        stop_reason: StopReason = StopReason.PENDING,
        error_message: str | None = None,
        raw_stop_reason: str | None = None,
    ) -> AssistantMessage:
        return AssistantMessage(
            content=list(self.blocks),
            usage=self.usage,
            stop_reason=stop_reason,
            error_message=error_message,
            raw_stop_reason=raw_stop_reason,
        )


class ProviderCoreStream:
    """One assistant-message stream: ``async for frame in stream`` + ``result()``."""

    def __init__(self, provider: Provider, request: ProviderRequest, *, cancellation: Any = None) -> None:
        self._provider = provider
        self._request = request
        self._cancellation = cancellation
        self._final: AssistantMessage | None = None
        self._frames = self._iterate()

    # -- the two halves of the core contract ---------------------------

    def __aiter__(self) -> "ProviderCoreStream":
        return self

    async def __anext__(self) -> Mapping[str, Any]:
        return await self._frames.__anext__()

    async def result(self) -> AssistantMessage:
        """The final message. Available once a terminal frame has been produced."""
        if self._final is None:
            raise ContractError("the provider stream has not produced a terminal frame yet")
        return self._final

    # -- framing -------------------------------------------------------

    async def _iterate(self) -> AsyncIterator[Mapping[str, Any]]:
        blocks = _Blocks()
        active: str | None = None

        def frame(kind: str, **extra: Any) -> Mapping[str, Any]:
            return {"type": kind, "partial": blocks.message(), **extra}

        yield frame("start")

        def end_active() -> Mapping[str, Any] | None:
            nonlocal active
            if active is None:
                return None
            finished = frame(f"{active}_end")
            active = None
            return finished

        final: AssistantMessage
        events = self._provider.stream(self._request, self._cancellation)
        try:
            try:
                async for event in events:
                    if event.kind is ProviderEventKind.TEXT_DELTA:
                        if active != "text":
                            ending = end_active()
                            if ending is not None:
                                yield ending
                            blocks.begin_text()
                            active = "text"
                            yield frame("text_start")
                        blocks.add_text(event.text)
                        yield frame("text_delta", text=event.text)
                    elif event.kind is ProviderEventKind.THINKING_DELTA:
                        if active != "thinking":
                            ending = end_active()
                            if ending is not None:
                                yield ending
                            blocks.begin_thinking()
                            active = "thinking"
                            yield frame("thinking_start")
                        blocks.add_thinking(event.thinking, _signature_of(event))
                        yield frame("thinking_delta", thinking=event.thinking)
                    elif event.kind is ProviderEventKind.TOOL_CALL:
                        ending = end_active()
                        if ending is not None:
                            yield ending
                        arguments = _arguments_of(event)
                        blocks.begin_tool_call(event.tool_call_id, event.tool_name)
                        yield frame("toolcall_start", tool_call_id=event.tool_call_id)
                        blocks.complete_tool_call(arguments)
                        yield frame("toolcall_end", tool_call_id=event.tool_call_id, arguments=arguments)
                    elif event.kind is ProviderEventKind.USAGE:
                        blocks.usage = core_usage(event.usage)
                    elif event.kind is ProviderEventKind.ERROR:
                        failure = event.failure_kind or ProviderFailureKind.FAILED
                        final = blocks.message(
                            stop_reason=FAILURE_STOP_REASON.get(failure, StopReason.ERROR),
                            error_message=_failure_message(failure, event.failure_detail),
                            raw_stop_reason=failure.value,
                        )
                        self._final = final
                        ending = end_active()
                        if ending is not None:
                            yield ending
                        yield {"type": "error", "partial": final, "error": final.error_message}
                        return
                    elif event.kind is ProviderEventKind.TERMINAL:
                        stop_reason = map_stop_reason(event.stop_reason)
                        if stop_reason is StopReason.ERROR:
                            # An unread finish reason is not a finished answer.
                            final = blocks.message(
                                stop_reason=StopReason.ERROR,
                                error_message=(
                                    f"provider reported an unrecognised finish reason {event.stop_reason!r}; "
                                    "refusing to treat it as a completed answer"
                                ),
                                raw_stop_reason=event.stop_reason,
                            )
                            self._final = final
                            ending = end_active()
                            if ending is not None:
                                yield ending
                            yield {"type": "error", "partial": final, "error": final.error_message}
                            return
                        final = blocks.message(stop_reason=stop_reason, raw_stop_reason=event.stop_reason)
                        self._final = final
                        ending = end_active()
                        if ending is not None:
                            yield ending
                        yield {"type": "done", "partial": final}
                        return
            except asyncio.CancelledError:
                # A cancelled task is not a provider failure: keep the partial
                # answer readable, then let asyncio's cancellation propagate.
                if self._final is None:
                    self._final = blocks.message(
                        stop_reason=StopReason.ABORTED, error_message="cancelled"
                    )
                raise
            except ProviderFailure as failure:
                kind = _failure_kind(failure)
                final = blocks.message(
                    stop_reason=FAILURE_STOP_REASON.get(kind, StopReason.ERROR),
                    error_message=_failure_message(kind, failure.detail),
                    raw_stop_reason=kind.value,
                )
            except Exception as failure:  # noqa: BLE001 - a broken provider must not fake a turn
                final = blocks.message(
                    stop_reason=StopReason.ERROR,
                    error_message=f"provider stream failed: {type(failure).__name__}: {failure}",
                    raw_stop_reason=ProviderFailureKind.FAILED.value,
                )
            else:
                # The stream ran out without a terminal frame. Reporting "stop"
                # here would turn a truncated answer into a finished one.
                final = blocks.message(
                    stop_reason=StopReason.ERROR,
                    error_message=(
                        "provider stream ended without a terminal frame; "
                        "refusing to report a finished answer"
                    ),
                    raw_stop_reason=ProviderFailureKind.INVALID_OUTPUT.value,
                )
        finally:
            # Close the provider stream as soon as the terminal frame is known.
            # Leaving it to the garbage collector means its pump — and the
            # connection it opened — can be torn down during the *next* turn,
            # which would close the next request's connection instead.
            close = getattr(events, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

        self._final = final
        ending = end_active()
        if ending is not None:
            yield ending
        yield {"type": "error", "partial": final, "error": final.error_message}


def _signature_of(event: ProviderEvent) -> str | None:
    value = event.private.get("signature") if event.private else None
    return str(value) if value else None


def _arguments_of(event: ProviderEvent) -> Mapping[str, Any]:
    raw = event.arguments_json or "{}"
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ContractError(
            f"tool call {event.tool_name or event.tool_call_id!r} arrived with unparseable arguments: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ContractError(
            f"tool call {event.tool_name or event.tool_call_id!r} arguments must be a JSON object, "
            f"got {type(parsed).__name__}"
        )
    return parsed


def _failure_kind(failure: ProviderFailure) -> ProviderFailureKind:
    try:
        return ProviderFailureKind(failure.kind)
    except ValueError:
        return ProviderFailureKind.FAILED


def _failure_message(kind: ProviderFailureKind, detail: str) -> str:
    return f"{kind.value}: {detail}" if detail else kind.value


@dataclass
class CoreStreamBridge:
    """A ``stream_fn`` for :class:`~yanxu_harness.core.agent_loop.AgentLoopConfig`.

    ``send_thinking`` re-sends stored thinking material to the endpoint; it is off
    by default because most OpenAI-compatible endpoints reject it.
    """

    provider: Provider
    max_output_tokens: int | None = None
    send_thinking: bool = False

    async def __call__(
        self, model: Model | None, context: TranscriptContext, options: Any = None
    ) -> ProviderCoreStream:
        request = self.build_request(model, context, options)
        signal = (options or {}).get("signal")
        return ProviderCoreStream(self.provider, request, cancellation=signal)

    def build_request(
        self, model: Model | None, context: TranscriptContext, options: Any = None
    ) -> ProviderRequest:
        # Built here, before the first frame: a transcript this transport cannot
        # express is a host error that must surface immediately, not mid-stream.
        options = options or {}
        if options.get("api_key"):
            # One credential entry point. Core can carry a resolved key
            # (`AgentLoopConfig.api_key`), but this bridge resolves secrets
            # through the provider's own `SecretReference`; accepting both would
            # make "which key was used" unanswerable from the config alone.
            raise ContractError(
                "this bridge resolves credentials from ProviderConfig/its secret source; "
                "an api_key in the core options would be a second, silent credential path"
            )
        return to_provider_request(
            model,
            context,
            options,
            max_output_tokens=self.max_output_tokens,
            send_thinking=self.send_thinking,
        )
