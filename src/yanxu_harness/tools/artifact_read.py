"""Reading a stored original is a tool call, not a privilege.

A bounded view tells the model that the rest of a result exists at ``artifact_ref=…``. This
module is the other half: one READ-effect tool definition that fetches it, so the read goes
through the same gateway, policy, approval binding and receipt path as every other call.

What this deliberately is **not**:

* not a file reader — the argument is a content id the store itself issued, and
  :func:`~yanxu_harness.context.artifacts.read_artifact` refuses anything else, so a mention
  of a path in a transcript cannot become a read;
* not a permission grant — the definition declares ``ToolEffect.READ``, so a deployment's
  policy decides whether this agent may read at all, and the approval path can gate it;
* not unbounded — the tool has its own output cap, because the reason a result was bounded
  in the first place was its size. It says what it returned and what it left out.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..context.artifacts import ArtifactStore, read_artifact
from ..errors import ContractError
from ..versioning import fingerprint
from .definition import ToolDefinition, ToolEffect

__all__ = ["ARTIFACT_READ_TOOL", "artifact_read_definition"]

#: The default name a bounded view points at (``artifact_ref=…`` + "the artifact read tool").
ARTIFACT_READ_TOOL = "artifact.read"

#: How much of an original one read returns. Beyond this the tool returns a slice *and says
#: so*, so a large artifact can be walked in bounded steps instead of flooding the window.
DEFAULT_MAX_CHARS = 20_000


def artifact_read_definition(
    store: ArtifactStore,
    *,
    name: str = ARTIFACT_READ_TOOL,
    max_chars: int = DEFAULT_MAX_CHARS,
    description: str = (
        "Read back the original content of a tool result that was shown to you in bounded "
        "form. Pass the artifact_ref from that view. Returns at most max_chars characters; "
        "when the original is larger the answer says which range it returned."
    ),
) -> ToolDefinition:
    """One READ-effect tool that returns the stored original behind a reference."""
    if max_chars <= 0:
        raise ContractError("max_chars must be positive")

    def handler(_context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        artifact_id = str(arguments["artifact_ref"])
        offset = int(arguments.get("offset") or 0)
        if offset < 0:
            raise ContractError("offset must not be negative")
        content = read_artifact(store, artifact_id)
        total = len(content)
        if offset > total:
            raise ContractError(
                f"offset {offset} is past the end of {artifact_id} ({total} characters)"
            )
        window = content[offset : offset + max_chars]
        truncated = offset + len(window) < total
        header = (
            f"[artifact {artifact_id}: {total} characters total, returning characters "
            f"{offset}-{offset + len(window)}"
            + (", more available]" if truncated else "]")
        )
        body = f"{header}\n{window}"
        if truncated:
            body += (
                f"\n[next offset: {offset + len(window)}; artifact_ref={artifact_id}]"
            )
        return {
            "content": [{"type": "text", "text": body}],
            "details": {
                "artifact_id": artifact_id,
                "total_chars": total,
                "returned_from": offset,
                "returned_chars": len(window),
                "truncated": truncated,
                "content_fingerprint": fingerprint({"artifact": artifact_id}),
            },
        }

    def validator(arguments: Mapping[str, Any]) -> dict[str, Any]:
        reference = arguments.get("artifact_ref")
        if not isinstance(reference, str) or not reference.startswith("sha256:"):
            raise ValueError(
                "artifact_ref must be a content id like 'sha256:…' — a path, URL or other "
                "handle is not an artifact reference"
            )
        resolved: dict[str, Any] = {"artifact_ref": reference}
        if arguments.get("offset") is not None:
            resolved["offset"] = int(arguments["offset"])
        return resolved

    return ToolDefinition(
        name,
        description,
        effects=frozenset({ToolEffect.READ}),
        args_validator=validator,
        handler=handler,
        preview=lambda _context, arguments: {
            "artifact_ref": str(arguments.get("artifact_ref", "")),
            "offset": int(arguments.get("offset") or 0),
        },
    )
