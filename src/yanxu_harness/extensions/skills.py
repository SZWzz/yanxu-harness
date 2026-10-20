"""The composite SkillProvider published by an assembly.

It is a *restricted view*, not a second skill store:

* metadata is listed and frozen during open (bounded), so a later listing cannot
  grow because a filesystem changed;
* bodies and resources are read lazily from the source provider, then checked
  against the frozen fingerprint; a changed source invalidates the deployment and
  refuses the read instead of silently serving new bytes;
* a request for another scope returns nothing — the source provider's ``global``
  fallback is deliberately not re-exported across namespaces;
* a skill still grants no tool, approval or scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Sequence

from ..skills.provider import (
    SkillBody,
    SkillBudgetExceeded,
    SkillLimits,
    SkillMetadata,
    SkillNotFound,
    SkillProvider,
    SkillResource,
    SkillResourceDenied,
    normalize_scope,
    safe_resource_path,
)
from .errors import ExtensionInvalidated
from .lifecycle import InvalidationToken

__all__ = ["CompositeSkillProvider", "SkillEntry"]


@dataclass(frozen=True)
class SkillEntry:
    """One frozen skill source in the published view."""

    skill_id: str
    metadata: SkillMetadata
    fingerprint: str
    provider: SkillProvider
    source_scope: str
    effective_scope: str
    source: str


class CompositeSkillProvider:
    """The four-interface SkillProvider the deployment publishes."""

    def __init__(
        self,
        *,
        scope: str,
        entries: Sequence[SkillEntry],
        limits: SkillLimits,
        token: InvalidationToken,
        on_invalidate: Callable[[str], None] | None = None,
    ) -> None:
        self._scope = normalize_scope(scope)
        self._entries = {entry.skill_id: entry for entry in entries}
        self._limits = limits
        self._token = token
        self._on_invalidate = on_invalidate

    # -- introspection --------------------------------------------------

    @property
    def limits(self) -> SkillLimits:
        return self._limits

    @property
    def scope(self) -> str:
        return self._scope

    def registered_ids(self, *, scope: str | None = None) -> tuple[str, ...]:
        if self._refusal() is not None:
            return ()
        wanted = normalize_scope(scope) if scope is not None else self._scope
        if wanted != self._scope:
            return ()
        return tuple(sorted(self._entries))

    # -- port -----------------------------------------------------------

    async def list_metadata(self, *, scope: str | None = None) -> Sequence[SkillMetadata]:
        self._raise_if_invalid()
        wanted = normalize_scope(scope) if scope is not None else self._scope
        if wanted != self._scope:
            return ()
        return tuple(self._entries[skill_id].metadata for skill_id in sorted(self._entries))

    async def load_body(self, skill_id: str, *, scope: str | None = None) -> SkillBody:
        self._raise_if_invalid()
        entry = self._resolve(skill_id, scope)
        body = await entry.provider.load_body(skill_id, scope=entry.source_scope)
        if body.fingerprint != entry.fingerprint:
            self._drift(entry, "body fingerprint")
        if len(body.text) > self._limits.body_chars:
            raise SkillBudgetExceeded(
                f"{skill_id}: body is {len(body.text)} chars, limit is {self._limits.body_chars}"
            )
        return SkillBody(
            skill_id=skill_id,
            version=body.version or entry.metadata.version,
            text=body.text,
            fingerprint=entry.fingerprint,
            scope=self._scope,
            resources=tuple(sorted(body.resources)),
            metadata=dict(entry.metadata.__dict__),
        )

    async def read_resource(
        self, skill_id: str, path: str, *, scope: str | None = None
    ) -> SkillResource:
        self._raise_if_invalid()
        entry = self._resolve(skill_id, scope)
        try:
            canonical = safe_resource_path(path)
        except SkillResourceDenied as exc:
            raise SkillResourceDenied(skill_id, path, exc.reason) from exc
        resource = await entry.provider.read_resource(skill_id, canonical, scope=entry.source_scope)
        if resource.path != canonical:
            self._drift(entry, "resource path")
        if resource.byte_length > self._limits.resource_bytes:
            raise SkillBudgetExceeded(
                f"{skill_id}: resource {canonical} is {resource.byte_length} bytes, "
                f"limit is {self._limits.resource_bytes}"
            )
        current = await entry.provider.fingerprint(skill_id, scope=entry.source_scope)
        if current != entry.fingerprint:
            self._drift(entry, "source fingerprint")
        return SkillResource(
            skill_id=skill_id,
            path=canonical,
            text=resource.text,
            fingerprint=resource.fingerprint,
            byte_length=resource.byte_length,
        )

    async def fingerprint(self, skill_id: str, *, scope: str | None = None) -> str:
        self._raise_if_invalid()
        return self._resolve(skill_id, scope).fingerprint

    # -- internals ------------------------------------------------------

    def _resolve(self, skill_id: str, scope: str | None) -> SkillEntry:
        wanted = normalize_scope(scope) if scope is not None else self._scope
        if wanted != self._scope:
            raise SkillNotFound(skill_id, wanted)
        entry = self._entries.get(skill_id)
        if entry is None:
            raise SkillNotFound(skill_id, wanted)
        return entry

    def _raise_if_invalid(self) -> None:
        reason = self._refusal()
        if reason is not None:
            raise ExtensionInvalidated(reason)

    def _refusal(self) -> str | None:
        if self._token.valid:
            return None
        return self._token.reason

    def _drift(self, entry: SkillEntry, what: str) -> None:
        reason = (
            f"skill {entry.skill_id!r} from {entry.source} changed its {what} after the "
            "deployment froze it; the deployment is invalidated instead of serving new content"
        )
        self._token.invalidate(reason)
        if self._on_invalidate is not None:
            self._on_invalidate(reason)
        raise ExtensionInvalidated(reason)


def metadata_size(entries: Sequence[SkillEntry]) -> int:
    """Canonical byte size of the frozen metadata view (used by the assembly bound)."""
    import json

    payload = [
        {
            "skill_id": entry.metadata.skill_id,
            "name": entry.metadata.name,
            "description": entry.metadata.description,
            "version": entry.metadata.version,
            "when_to_use": entry.metadata.when_to_use,
            "avoid_when": entry.metadata.avoid_when,
            "tools": list(entry.metadata.tools),
            "scope": entry.metadata.scope,
            "fingerprint": entry.metadata.fingerprint,
        }
        for entry in sorted(entries, key=lambda item: item.skill_id)
    ]
    return len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
