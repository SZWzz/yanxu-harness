"""An OpenAI-compatible streaming provider, standard library only.

Conventions this adapter is built around, all of which are testable against a
loopback server rather than a real vendor:

* **Streaming is the only mode.** Frames are parsed as they arrive; the final
  result is assembled from the same frames the caller already saw, so a caller
  that only reads deltas and a caller that only reads the result can never
  disagree about what the model said.
* **Interleaving is preserved.** Tool-call argument fragments are indexed; two
  tool calls whose fragments interleave must not be concatenated into one.
* **Failure is machine-readable.** HTTP status and in-band error bodies are
  mapped to :class:`ProviderFailureKind` values, so a host can tell rate limiting
  from an exhausted balance from a real context overflow.
* **Private frames stay private.** ``reasoning_content`` becomes a thinking
  delta; signatures and opaque blobs land in ``ProviderEvent.private`` and never
  in a public payload.
* **No secret in the payload.** The API key travels as a header only, and error
  text is redacted before it becomes a message.

Transport is injectable: the default uses ``http.client``, and tests point it at
a loopback server. Nothing here hard-codes a brand, an endpoint or a header
value — those arrive in :class:`ProviderConfig`.
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

from ..errors import ContractError, ProviderFailure
from .protocol import (
    ProviderConfig,
    ProviderEvent,
    ProviderEventKind,
    ProviderFailureKind,
    ProviderRequest,
    ProviderResult,
    ProviderToolSpec,
)

__all__ = [
    "OpenAICompatibleProvider",
    "PayloadGate",
    "collect",
    "StreamTransport",
    "HttpTransport",
    "classify_status",
    "parse_stream_frames",
    "redact",
]

#: Body fragments that mean "the request did not fit", independent of status code.
_OVERFLOW_PATTERNS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "input is too long",
)
_REDACT_PATTERNS = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)(\S+)"),
)


def redact(text: str) -> str:
    """Remove credential-shaped substrings before text becomes a message or a log."""
    cleaned = text
    for pattern in _REDACT_PATTERNS:
        cleaned = pattern.sub(lambda match: match.group(1) + "<redacted>" if match.lastindex and match.lastindex > 1 else "<redacted>", cleaned)
    return cleaned


def classify_status(status: int, body: str) -> ProviderFailureKind:
    """Map one HTTP failure onto the neutral kind, using the body when it is structured."""
    lowered = (body or "").lower()
    overflow = any(pattern in lowered for pattern in _OVERFLOW_PATTERNS)
    if status == 429:
        if "quota" in lowered or "credit" in lowered or "balance" in lowered:
            return ProviderFailureKind.CREDITS_EXHAUSTED
        return ProviderFailureKind.RATE_LIMITED
    if status in {401, 403}:
        return ProviderFailureKind.AUTH_FAILED
    if status == 404:
        return ProviderFailureKind.MODEL_NOT_FOUND
    if status == 402:
        return ProviderFailureKind.CREDITS_EXHAUSTED
    if status == 413:
        return ProviderFailureKind.CONTEXT_OVERFLOW if overflow else ProviderFailureKind.FAILED
    if status == 400:
        return ProviderFailureKind.CONTEXT_OVERFLOW if overflow else ProviderFailureKind.BAD_REQUEST
    if status >= 500:
        return ProviderFailureKind.FAILED
    return ProviderFailureKind.FAILED


@dataclass(frozen=True)
class WireResponse:
    """One transport response: the status, and a lazy iterator of body chunks."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: Iterable[bytes] = ()


class PayloadGate(Protocol):
    """Checks the final serialized body. Raises to refuse the dispatch."""

    def __call__(self, body: bytes, request: ProviderRequest) -> None:
        """Raise (for example :class:`~yanxu_harness.errors.BudgetExceeded`) to stop."""


class StreamTransport(Protocol):
    """Sends one request and returns the raw response, without interpreting it."""

    def send(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> WireResponse:
        """Perform the request. Must not retry: a retry could duplicate a side effect."""


class HttpTransport:
    """The default transport: stdlib ``http.client``, one attempt, no retries."""

    def send(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> WireResponse:
        import http.client

        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ContractError(f"provider base URL {url!r} is not an absolute http(s) URL")
        connection_cls = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_cls(parts.hostname, parts.port, timeout=timeout)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        try:
            connection.request("POST", path, body=body, headers=dict(headers))
            response = connection.getresponse()
            status = response.status
            response_headers = {key.lower(): value for key, value in response.getheaders()}

            def chunks() -> Iterator[bytes]:
                try:
                    while True:
                        line = response.readline()
                        if not line:
                            break
                        yield line
                finally:
                    connection.close()

            return WireResponse(status=status, headers=response_headers, chunks=chunks())
        except socket.timeout as exc:
            connection.close()
            raise ProviderFailure(ProviderFailureKind.TIMEOUT.value, str(exc)) from exc
        except OSError as exc:
            connection.close()
            raise ProviderFailure(ProviderFailureKind.FAILED.value, redact(str(exc))) from exc


def parse_stream_frames(lines: Iterable[bytes]) -> Iterator[Mapping[str, Any]]:
    """Turn an SSE byte stream into provider frames.

    Comments (``:`` ping lines), blank keep-alives and the ``[DONE]`` sentinel are
    ignored rather than turned into text — a heartbeat is not content.
    """
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        if not line or line == "[DONE]":
            continue
        try:
            frame = json.loads(line)
        except ValueError as exc:
            raise ProviderFailure(
                ProviderFailureKind.INVALID_OUTPUT.value,
                f"unparseable provider frame: {exc}; frame={line[:200]!r}",
            ) from exc
        if isinstance(frame, Mapping):
            yield frame


@dataclass
class _ToolCallBuffer:
    index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""

    def as_dict(self) -> Mapping[str, Any]:
        return {"id": self.call_id, "name": self.name, "arguments": self.arguments}


class OpenAICompatibleProvider:
    """A streaming provider speaking the OpenAI chat-completions protocol."""

    name = "openai-compatible"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        secrets: Mapping[str, str] | Any | None = None,
        transport: StreamTransport | None = None,
        batch_flush_chars: int = 512,
        payload_gate: "PayloadGate | None" = None,
    ) -> None:
        self.config = config
        self._secrets = secrets
        self._transport = transport or HttpTransport()
        self._batch_flush_chars = batch_flush_chars
        #: Measured against the *final* serialized body, immediately before
        #: dispatch. A gate that only looks at an earlier estimate cannot catch a
        #: payload that grew while it was being assembled.
        self._payload_gate = payload_gate

    # -- request building ----------------------------------------------

    def build_body(self, request: ProviderRequest) -> Mapping[str, Any]:
        """Build the wire body. Exposed because the exact payload is a contract."""
        body: dict[str, Any] = {
            "model": request.model or self.config.model,
            "messages": list(request.messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.system:
            body["messages"] = [{"role": "system", "content": request.system}, *body["messages"]]
        if request.tools:
            body["tools"] = [_tool_payload(spec) for spec in request.tools]
            body["tool_choice"] = "auto"
        if request.max_output_tokens is not None:
            body["max_tokens"] = request.max_output_tokens
        for key, value in self.config.options.items():
            body.setdefault(key, value)
        return body

    def build_headers(self) -> Mapping[str, str]:
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        headers.update(self.config.headers)
        if self.config.api_key is not None and self._secrets is not None:
            headers.setdefault("authorization", f"Bearer {self.config.api_key.resolve(self._secrets)}")
        return headers

    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if not base:
            raise ContractError("provider config requires a base_url")
        return f"{base}/chat/completions"

    # -- streaming ------------------------------------------------------

    async def stream(self, request: ProviderRequest, cancellation: Any = None) -> Any:
        """Yield neutral frames. Implemented as an async generator."""
        for event in self._frames(request, cancellation):
            if cancellation is not None and getattr(cancellation, "cancelled", False):
                yield ProviderEvent(
                    kind=ProviderEventKind.ERROR,
                    failure_kind=ProviderFailureKind.CANCELLED,
                    failure_detail="cancelled",
                )
                return
            yield event

    def _frames(self, request: ProviderRequest, cancellation: Any) -> Iterator[ProviderEvent]:
        body = json.dumps(self.build_body(request), ensure_ascii=False).encode("utf-8")
        if self._payload_gate is not None:
            # Refuse here and nothing is sent: no partial request, no retry, no
            # "it probably fit" reasoning.
            self._payload_gate(body, request)
        started = time.monotonic()
        try:
            response = self._transport.send(
                url=self.endpoint(),
                headers=self.build_headers(),
                body=body,
                timeout=self.config.timeout_seconds,
            )
        except ProviderFailure:
            raise
        if response.status >= 400:
            text = b"".join(response.chunks).decode("utf-8", errors="replace")
            kind = classify_status(response.status, text)
            raise ProviderFailure(kind.value, redact(text[:500]))

        sequence = 0
        buffers: dict[int, _ToolCallBuffer] = {}
        usage: Mapping[str, Any] = {}
        stop_reason = ""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        pending_text: list[str] = []

        for frame in parse_stream_frames(response.chunks):
            error = frame.get("error")
            if isinstance(error, Mapping):
                kind = classify_status(
                    int(error.get("code") or 400), json.dumps(error, ensure_ascii=False)
                )
                raise ProviderFailure(kind.value, redact(str(error.get("message", ""))[:500]))
            for choice in frame.get("choices") or ():
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                    pending_text.append(content)
                    if sum(len(part) for part in pending_text) >= self._batch_flush_chars:
                        yield ProviderEvent(
                            kind=ProviderEventKind.TEXT_DELTA,
                            sequence=sequence,
                            text="".join(pending_text),
                        )
                        sequence += 1
                        pending_text.clear()
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    thinking_parts.append(reasoning)
                    yield ProviderEvent(
                        kind=ProviderEventKind.THINKING_DELTA,
                        sequence=sequence,
                        thinking=reasoning,
                        private=_private_extras(delta),
                    )
                    sequence += 1
                for call in delta.get("tool_calls") or ():
                    index = int(call.get("index", 0))
                    buffer = buffers.setdefault(index, _ToolCallBuffer(index=index))
                    if call.get("id"):
                        buffer.call_id = str(call["id"])
                    function = call.get("function") or {}
                    if function.get("name"):
                        buffer.name = str(function["name"])
                    if function.get("arguments"):
                        buffer.arguments += str(function["arguments"])
                if choice.get("finish_reason"):
                    stop_reason = str(choice["finish_reason"])
            if frame.get("usage"):
                usage = dict(frame["usage"])

        if pending_text:
            yield ProviderEvent(
                kind=ProviderEventKind.TEXT_DELTA, sequence=sequence, text="".join(pending_text)
            )
            sequence += 1

        for buffer in (buffers[index] for index in sorted(buffers)):
            payload = buffer.as_dict()
            try:
                arguments = json.loads(buffer.arguments or "{}")
            except ValueError as exc:
                raise ProviderFailure(
                    ProviderFailureKind.INVALID_OUTPUT.value,
                    f"tool call {buffer.name or buffer.call_id!r} arguments are not JSON: {exc}",
                ) from exc
            yield ProviderEvent(
                kind=ProviderEventKind.TOOL_CALL,
                sequence=sequence,
                tool_call_id=buffer.call_id,
                tool_name=buffer.name,
                arguments_json=json.dumps(arguments, ensure_ascii=False),
            )
            sequence += 1

        if usage:
            yield ProviderEvent(
                kind=ProviderEventKind.USAGE, sequence=sequence, usage=_normalise_usage(usage)
            )
            sequence += 1
        if not stop_reason:
            # A stream that ended without a finish reason is not "stop": inventing
            # one would hide a truncated answer.
            raise ProviderFailure(
                ProviderFailureKind.INVALID_OUTPUT.value,
                f"provider stream ended without a finish reason after {time.monotonic() - started:.1f}s",
            )
        yield ProviderEvent(
            kind=ProviderEventKind.TERMINAL,
            sequence=sequence,
            stop_reason=stop_reason,
            usage=dict(usage),
        )


def _private_extras(delta: Mapping[str, Any]) -> Mapping[str, Any]:
    extras: dict[str, Any] = {}
    for key in ("signature", "encrypted_content", "reasoning_details"):
        if key in delta:
            extras[key] = delta[key]
    return extras


def _normalise_usage(usage: Mapping[str, Any]) -> Mapping[str, Any]:
    """Keep missing numbers missing; a fabricated 0 would look like a free request."""
    out: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        value = usage.get(key)
        if value is None:
            details = usage.get("completion_tokens_details") or {}
            value = details.get(key) if isinstance(details, Mapping) else None
        out[key] = value
    return out


def _tool_payload(spec: ProviderToolSpec) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": dict(spec.parameters or {"type": "object", "properties": {}}),
        },
    }
    if spec.strict:
        payload["function"]["strict"] = True
    return payload


async def collect(provider: OpenAICompatibleProvider, request: ProviderRequest) -> ProviderResult:
    """Drive one stream to completion and return the assembled result.

    A caller may use this instead of consuming frames; both paths see the same
    frames, so they cannot disagree about what the model said.
    """
    text: list[str] = []
    thinking: list[str] = []
    calls: list[Mapping[str, Any]] = []
    usage: Mapping[str, Any] = {}
    stop_reason = ""
    frames = 0
    async for event in provider.stream(request):
        frames += 1
        if event.kind is ProviderEventKind.TEXT_DELTA:
            text.append(event.text)
        elif event.kind is ProviderEventKind.THINKING_DELTA:
            thinking.append(event.thinking)
        elif event.kind is ProviderEventKind.TOOL_CALL:
            calls.append(
                {
                    "id": event.tool_call_id,
                    "name": event.tool_name,
                    "arguments": json.loads(event.arguments_json or "{}"),
                }
            )
        elif event.kind is ProviderEventKind.USAGE:
            usage = dict(event.usage)
        elif event.kind is ProviderEventKind.TERMINAL:
            stop_reason = event.stop_reason
        elif event.kind is ProviderEventKind.ERROR:
            event.raise_for_error()
    return ProviderResult(
        text="".join(text),
        thinking="".join(thinking),
        tool_calls=tuple(calls),
        usage=usage,
        stop_reason=stop_reason,
        frames=frames,
    )
