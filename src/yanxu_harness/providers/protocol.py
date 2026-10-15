"""The provider port: one streaming contract, host-supplied configuration.

The harness asks a provider for a stream of neutral events and a final result.
It does not know about brands, endpoints, credentials or headers — those arrive
in :class:`ProviderConfig`, which the *host* builds. Three rules are enforced
here rather than documented:

* **Credentials are references, not values.** :class:`SecretReference` holds the
  *name* of a secret; resolving it is the host's job and the resolved value never
  enters a payload, a repr, a log line or an event. :meth:`ProviderConfig.public`
  is the only shape that may be logged.
* **Failure is a state, not an exception soup.** Every failure carries a
  machine-readable kind so a host can tell rate limiting from an exhausted
  balance from a bad request, and can tell context overflow from a generic 400.
* **Private frames do not leave.** A provider may receive reasoning signatures,
  opaque blobs and provider headers; none of them may reach a public event. The
  harness drops them instead of forwarding them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from ..errors import ContractError, ProviderFailure
from ..runtime.context import Cancellation

__all__ = [
    "Provider",
    "ProviderConfig",
    "ProviderEvent",
    "ProviderEventKind",
    "ProviderFailureKind",
    "ProviderRequest",
    "ProviderResult",
    "ProviderToolSpec",
    "SecretReference",
]


class SecretReference:
    """A *name* for a secret. Never the secret itself.

    ``resolve`` is intentionally explicit: the harness never resolves one by
    itself, so a provider adapter cannot quietly read an environment variable,
    a file or a database column that the host did not hand it.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        if not name:
            raise ContractError("a secret reference needs a name")
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def resolve(self, source: Mapping[str, str] | Callable[[str], str | None]) -> str:
        """Resolve through the host-supplied mapping or callable."""
        value = source.get(self._name) if isinstance(source, Mapping) else source(self._name)
        if not value:
            raise ContractError(f"secret {self._name!r} is not available to this deployment")
        return value

    def __repr__(self) -> str:  # pragma: no cover - trivial, but must never leak
        return f"SecretReference({self._name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretReference) and other._name == self._name

    def __hash__(self) -> int:
        return hash(("SecretReference", self._name))


class ProviderFailureKind(str, Enum):
    """Machine-readable failure kinds a host can render and audit."""

    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    MODEL_NOT_FOUND = "model_not_found"
    BAD_REQUEST = "bad_request"
    CONTEXT_OVERFLOW = "context_overflow"
    CREDITS_EXHAUSTED = "credits_exhausted"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_OUTPUT = "invalid_output"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderConfig:
    """Everything a provider adapter needs, supplied by the host."""

    model: str
    base_url: str = ""
    api_key: SecretReference | None = None
    #: Transport headers (brand identification, session ids). Private: never logged.
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    #: Declared window, when the host knows it. ``None`` means unknown.
    context_limit_tokens: int | None = None
    output_reserve_tokens: int | None = None
    #: Optional extra body fields the provider protocol needs, e.g. reasoning flags.
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model:
            raise ContractError("provider config requires a model name")
        if self.timeout_seconds <= 0:
            raise ContractError("timeout_seconds must be positive")

    def public(self) -> Mapping[str, Any]:
        """The loggable projection. Contains no header values and no key material."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "has_api_key": self.api_key is not None,
            "api_key_name": self.api_key.name if self.api_key else None,
            "header_names": sorted(self.headers),
            "timeout_seconds": self.timeout_seconds,
            "context_limit_tokens": self.context_limit_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
        }


class ProviderEventKind(str, Enum):
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL = "tool_call"
    USAGE = "usage"
    TERMINAL = "terminal"
    ERROR = "error"


#: Payload keys a provider may attach that are *never* forwarded publicly.
PRIVATE_EVENT_FIELDS = frozenset({"signature", "encrypted_content", "reasoning_details", "raw"})


@dataclass(frozen=True)
class ProviderEvent:
    """One neutral frame from a provider stream."""

    kind: ProviderEventKind
    sequence: int = 0
    text: str = ""
    thinking: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    arguments_json: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    failure_kind: ProviderFailureKind | None = None
    failure_detail: str = ""
    #: Provider-internal extras (signatures, opaque blobs). Never published.
    private: Mapping[str, Any] = field(default_factory=dict)

    def public_payload(self) -> Mapping[str, Any]:
        """The publishable projection: private fields are dropped, not renamed."""
        payload: dict[str, Any] = {"kind": self.kind.value}
        if self.text:
            payload["text"] = self.text
        if self.thinking:
            payload["thinking"] = self.thinking
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        if self.arguments_json:
            payload["arguments_json"] = self.arguments_json
        if self.usage:
            payload["usage"] = dict(self.usage)
        if self.stop_reason:
            payload["stop_reason"] = self.stop_reason
        if self.failure_kind is not None:
            payload["failure_kind"] = self.failure_kind.value
            payload["failure_detail"] = self.failure_detail
        return payload

    def raise_for_error(self) -> None:
        if self.kind is ProviderEventKind.ERROR:
            kind = self.failure_kind or ProviderFailureKind.FAILED
            raise ProviderFailure(kind.value, self.failure_detail)


@dataclass(frozen=True)
class ProviderToolSpec:
    """The wire shape of one tool offered to the model."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    strict: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ContractError("a provider tool spec needs a name")


@dataclass(frozen=True)
class ProviderRequest:
    """One model request, already projected and budgeted by the caller."""

    messages: Sequence[Mapping[str, Any]]
    tools: Sequence[ProviderToolSpec] = ()
    system: str = ""
    max_output_tokens: int | None = None
    model: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ContractError("max_output_tokens must be positive when declared")


@dataclass(frozen=True)
class ProviderResult:
    """The final outcome of one provider stream."""

    text: str = ""
    thinking: str = ""
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    frames: int = 0


@runtime_checkable
class Provider(Protocol):
    """A streaming model provider. Configuration and credentials come from the host."""

    @property
    def name(self) -> str:
        """A stable adapter name, used in diagnostics."""

    async def stream(
        self, request: ProviderRequest, cancellation: Cancellation | None = None
    ) -> AsyncIterator[ProviderEvent]:
        """Yield neutral frames. Cancellation must stop the stream promptly."""
