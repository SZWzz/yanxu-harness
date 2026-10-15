"""C4: keep the original, bound the view, and let the model read it back on request.

The plan's rule is narrow and worth repeating: a large tool output is never *deleted* and
never silently shortened. The harness keeps the message it accepted; the model gets a
bounded view that says exactly what was left out and how to ask for the rest, and the
original lives in an immutable, content-addressed store behind a reference.

Three properties make that safe rather than a new way to lose data:

* **A reference is only written when its content is already stored.** If the store cannot
  hold the original, the bounded view is not produced and the projection refuses: an
  unreadable reference must never replace the only copy of the body. The store therefore
  *refuses* instead of evicting — an evicted blob would make a committed reference dangle,
  which is the same failure one step later.
* **A reference is not a path and grants nothing.** It is an opaque content id
  (``sha256:<hex>``), and reading it is a READ-effect tool call through the existing
  gateway, so the host's policy decides who may read it. Nothing here grants permission.
* **The substitution touches content only.** ``tool_call_id``, ``tool_name``, ``is_error``,
  the execution status, the arguments and the receipt facts are not this module's to
  change, and the projector's substitution is a content rewrite plus a recorded reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from ..errors import ContractError
from ..versioning import fingerprint

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "BoundedToolOutput",
    "InMemoryArtifactStore",
    "ToolOutputPolicy",
    "bounded_tool_output",
]


@dataclass(frozen=True)
class ArtifactRef:
    """A reference to immutable stored content.

    ``artifact_id`` is derived from the content (``sha256:<hex>``), so the same bytes are
    the same artifact and a reference can never point at something that changed. The
    remaining fields describe the original for the reader: its size, its fingerprint (the
    same id), its media type, and the call it came from, so a bounded view can name its own
    source range.
    """

    artifact_id: str
    byte_length: int
    media_type: str = "text/plain"
    source_call_id: str = ""
    source_tool_name: str = ""
    char_length: int = 0

    def __post_init__(self) -> None:
        if not self.artifact_id.startswith("sha256:"):
            raise ContractError(
                "an artifact reference must be a content id (sha256:<hex>), not a path or a "
                f"host-supplied handle: got {self.artifact_id!r}"
            )
        if self.byte_length < 0 or self.char_length < 0:
            raise ContractError("an artifact reference must not carry negative lengths")

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "byte_length": self.byte_length,
            "char_length": self.char_length,
            "media_type": self.media_type,
            "source_call_id": self.source_call_id,
            "source_tool_name": self.source_tool_name,
        }


@runtime_checkable
class ArtifactStore(Protocol):
    """Immutable, content-addressed storage for originals a view refers to.

    ``put`` either stores the whole content and returns its reference, or raises. It never
    stores a prefix, never overwrites and never evicts: a committed reference must stay
    readable, and a store that cannot guarantee that must say so before the reference is
    written into a run record.
    """

    def put(
        self,
        content: bytes,
        *,
        media_type: str = "text/plain",
        source_call_id: str = "",
        source_tool_name: str = "",
    ) -> ArtifactRef:
        """Store content immutably and return its reference. Raises if it cannot."""

    def get(self, artifact_id: str) -> bytes | None:
        """The exact bytes, or ``None`` when this store does not hold them."""

    def holds(self, artifact_id: str) -> bool:
        """Whether ``get`` would return content. Cheaper than reading it."""

    @property
    def durability(self) -> Any:
        """Whether a stored original survives this process.

        ``VOLATILE`` is a legitimate answer for a run that keeps its originals in memory —
        but it is a fact a committed reference depends on, so it is part of what a
        ``bounded_outputs`` entry records, and a host that needs references to outlive a
        restart asks for ``require_durable`` and gets a refusal instead of a dangling id.
        """


@dataclass
class InMemoryArtifactStore:
    """A bounded in-memory store that refuses rather than losing a referenced original.

    ``max_bytes`` is the total budget for stored content. When a put would exceed it the
    store raises instead of evicting: an unreferenced blob could be evicted safely, but
    this store cannot tell which references have been committed into a run record, so it
    treats every stored blob as committed. A host that needs eviction needs a store that
    can prove which references are still live — that is a different class, not a config
    flag here.
    """

    max_bytes: int = 8 * 1024 * 1024
    _content: dict[str, bytes] = field(default_factory=dict, repr=False)
    _refs: dict[str, ArtifactRef] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ContractError("max_bytes must be positive")

    # -- properties -----------------------------------------------------

    @property
    def durability(self) -> Any:
        """This store does not survive the process that filled it."""
        from ..storage.ports import Durability

        return Durability.VOLATILE

    @property
    def stored_bytes(self) -> int:
        return sum(len(content) for content in self._content.values())

    @property
    def count(self) -> int:
        return len(self._content)

    # -- the port -------------------------------------------------------

    def put(
        self,
        content: bytes,
        *,
        media_type: str = "text/plain",
        source_call_id: str = "",
        source_tool_name: str = "",
    ) -> ArtifactRef:
        if not isinstance(content, (bytes, bytearray)):
            raise ContractError(
                f"an artifact store holds bytes, got {type(content).__name__}; encode text first"
            )
        blob = bytes(content)
        artifact_id = fingerprint({"bytes": blob.hex()})
        existing = self._refs.get(artifact_id)
        if existing is not None:
            # Same content, same artifact: a second put is a no-op, not a second copy.
            return existing
        if self.stored_bytes + len(blob) > self.max_bytes:
            raise ContractError(
                f"the artifact store holds {self.stored_bytes} of {self.max_bytes} bytes and "
                f"cannot store {len(blob)} more; refusing instead of evicting an original that "
                "a committed reference may point at"
            )
        ref = ArtifactRef(
            artifact_id=artifact_id,
            byte_length=len(blob),
            media_type=media_type,
            source_call_id=source_call_id,
            source_tool_name=source_tool_name,
            char_length=len(blob.decode("utf-8", errors="replace")),
        )
        self._content[artifact_id] = blob
        self._refs[artifact_id] = ref
        return ref

    def get(self, artifact_id: str) -> bytes | None:
        return self._content.get(artifact_id)

    def holds(self, artifact_id: str) -> bool:
        return artifact_id in self._content

    def ref(self, artifact_id: str) -> ArtifactRef | None:
        return self._refs.get(artifact_id)


@dataclass(frozen=True)
class ToolOutputPolicy:
    """How much of one tool result the model may see when the window is tight."""

    #: A result at or below this many characters is never bounded.
    max_chars: int = 4000
    head_chars: int = 1200
    tail_chars: int = 800
    #: When true, a reference may only be committed against a store that survives the
    #: process. A volatile store then produces no reference at all: the projection refuses
    #: and says why, instead of leaving an id nothing can resolve after a crash.
    require_durable: bool = False

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ContractError("max_chars must be positive")
        if self.head_chars < 0 or self.tail_chars < 0:
            raise ContractError("head_chars and tail_chars must not be negative")
        if self.head_chars + self.tail_chars >= self.max_chars:
            raise ContractError(
                "head_chars + tail_chars must be smaller than max_chars, otherwise the bounded "
                "view would be as large as the limit it exists to enforce"
            )


@dataclass(frozen=True)
class BoundedToolOutput:
    """A bounded view of one tool result, plus the reference that leads to the original."""

    text: str
    ref: ArtifactRef
    original_chars: int
    omitted_chars: int
    head_chars: int
    tail_chars: int
    #: Whether the store holding the original survives this process. Part of the committed
    #: record because "the rest is over there" is only useful if "over there" still exists.
    store_durability: str = "unknown"

    def as_dict(self) -> Mapping[str, Any]:
        return {
            **dict(self.ref.as_dict()),
            "original_chars": self.original_chars,
            "omitted_chars": self.omitted_chars,
            "head_chars": self.head_chars,
            "tail_chars": self.tail_chars,
            "store_durability": self.store_durability,
        }


def bounded_tool_output(
    content: str,
    *,
    policy: ToolOutputPolicy,
    store: ArtifactStore,
    call_id: str,
    tool_name: str,
    media_type: str = "text/plain",
) -> BoundedToolOutput:
    """Store the original and build the bounded view that replaces it in a request.

    The order matters and is the whole safety property: the content is stored *first*, so a
    reference in a committed record always has readable bytes behind it. If the store
    refuses, this raises and the caller refuses the projection — it never produces a view
    that says "the rest is at <ref>" when nothing is at ``<ref>``.

    The view keeps the beginning (usually the answer) and the end (usually the error or the
    summary), names the exact number of characters left out, and states the one call that
    can fetch the rest. It is text, not a protocol object: what the model sees is what the
    operator sees in a log.
    """
    if len(content) <= policy.max_chars:
        raise ContractError(
            "bounded_tool_output is only for content over the policy limit; refusing to wrap "
            "a result that already fits"
        )
    durability = getattr(getattr(store, "durability", None), "value", "unknown")
    if policy.require_durable and durability != "durable":
        raise ContractError(
            f"this deployment requires references to outlive the process, but the artifact "
            f"store is {durability}; refusing to refer to an original that would disappear"
        )
    ref = store.put(
        content.encode("utf-8"),
        media_type=media_type,
        source_call_id=call_id,
        source_tool_name=tool_name,
    )
    if not store.holds(ref.artifact_id):
        # A store that returns a reference it cannot read is broken, and trusting it here
        # would put an unreadable pointer in a run record.
        raise ContractError(
            "the artifact store returned a reference it does not hold; refusing to refer to "
            "content that cannot be read back"
        )
    head = content[: policy.head_chars]
    tail = content[len(content) - policy.tail_chars :] if policy.tail_chars else ""
    omitted = len(content) - len(head) - len(tail)
    parts = [
        (
            f"[tool result truncated: {len(content)} characters total, "
            f"fingerprint {ref.artifact_id}, artifact_ref={ref.artifact_id}]"
        ),
        f"--- head ({len(head)} characters) ---",
        head,
        f"--- {omitted} characters omitted ---",
    ]
    if tail:
        parts.extend([f"--- tail ({len(tail)} characters) ---", tail])
    parts.append(
        f"[the full result is stored as {ref.artifact_id}; read it with the artifact read "
        f"tool using artifact_ref={ref.artifact_id}]"
    )
    text = "\n".join(parts)
    return BoundedToolOutput(
        text=text,
        ref=ref,
        original_chars=len(content),
        omitted_chars=omitted,
        head_chars=len(head),
        tail_chars=len(tail),
        store_durability=durability,
    )


def read_artifact(store: ArtifactStore, artifact_id: str, *, encoding: str = "utf-8") -> str:
    """Read an original back. Refuses a reference this store does not hold.

    Deliberately not a file path and not a URL: the only thing this accepts is a content id
    the store itself issued, so an untrusted model cannot turn a mention of a path into a
    read. Whether a *caller* may read is a policy decision made around this function (the
    shipped reader is a READ-effect tool through the gateway).
    """
    if not isinstance(artifact_id, str) or not artifact_id.startswith("sha256:"):
        raise ContractError(
            f"{artifact_id!r} is not an artifact reference; only a content id issued by the "
            "store can be read"
        )
    content = store.get(artifact_id)
    if content is None:
        raise ContractError(
            f"the artifact store does not hold {artifact_id}; nothing can be read for this "
            "reference"
        )
    return content.decode(encoding, errors="replace")


def artifact_records(bounded: Mapping[str, BoundedToolOutput]) -> Mapping[str, Any]:
    """The plain-data record of the substitutions a projection made (one per call)."""
    return {call_id: dict(view.as_dict()) for call_id, view in bounded.items()}


def apply_bounded_views(
    messages: Any,
    replacements: Mapping[str, str],
) -> tuple[Any, ...]:
    """Replace the *content* of tool results, leaving every protocol field alone.

    ``replacements`` maps ``tool_call_id`` to the bounded text. Only ``content`` is
    rewritten: ``tool_call_id``, ``tool_name``, ``is_error``, ``details`` and ``usage``
    travel unchanged, because a view that changed them would be a different fact about the
    call rather than a smaller rendering of the same one.
    """
    out: list[Any] = []
    for message in messages:
        call_id = getattr(message, "tool_call_id", None)
        if call_id is None and isinstance(message, Mapping):
            call_id = message.get("tool_call_id")
        replacement = replacements.get(str(call_id)) if call_id else None
        if replacement is None:
            out.append(message)
            continue
        if isinstance(message, Mapping):
            updated = dict(message)
            updated["content"] = replacement
            out.append(updated)
            continue
        from ..core.types import TextContent
        from dataclasses import replace as _replace

        out.append(_replace(message, content=[TextContent(text=replacement)]))
    return tuple(out)


def tool_result_text(message: Any) -> str:
    """The visible text of one tool result, whatever shape the message is in."""
    from .projection import visible_text

    return visible_text(message)


def is_tool_result(message: Any) -> bool:
    role = message.get("role") if isinstance(message, Mapping) else getattr(message, "role", None)
    return role in ("toolResult", "tool")


#: The call id a tool result answers, or ``None``.
def result_call_id(message: Any) -> str | None:
    if not is_tool_result(message):
        return None
    value = (
        message.get("tool_call_id")
        if isinstance(message, Mapping)
        else getattr(message, "tool_call_id", None)
    )
    return str(value) if value else None


def result_tool_name(message: Any) -> str:
    value = (
        message.get("tool_name")
        if isinstance(message, Mapping)
        else getattr(message, "tool_name", "")
    )
    return str(value or "")


#: A hook a host can use to decide which results may be bounded (e.g. never bound errors).
BoundedWhen = Callable[[Any], bool]
