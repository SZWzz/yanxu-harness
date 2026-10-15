"""An in-memory skill provider, and the reference behaviour of the port.

It exists for three reasons: hosts embed it, the package's own tests need a
provider that is not a filesystem, and it documents the port's expectations by
implementing them — bounded metadata listing, on-demand body/resource loading,
scope isolation, resource path confinement and budget enforcement.

Scope isolation is the subtle one. A skill registered in scope ``"project-a"``
is invisible from ``"project-b"``; only ``"global"`` skills are visible
everywhere. Without that, one project's method text would leak into another
project's prompt, which is exactly the cross-project leak the host contract
forbids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .provider import (
    SkillBody,
    SkillBudgetExceeded,
    SkillLimits,
    SkillMetadata,
    SkillNotFound,
    SkillResource,
    SkillResourceDenied,
    fingerprint_text,
    normalize_scope,
    safe_resource_path,
)

__all__ = ["InMemorySkillProvider", "SkillRecord"]


@dataclass(frozen=True)
class SkillRecord:
    """One registered skill: metadata, body and resources."""

    metadata: SkillMetadata
    body: str
    resources: Mapping[str, str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        parts = [self.body]
        for path in sorted(self.resources):
            parts.append(f"{path}\0{self.resources[path]}")
        return fingerprint_text("\0".join(parts))


class InMemorySkillProvider:
    """A volatile :class:`~yanxu_harness.skills.provider.SkillProvider`."""

    def __init__(self, limits: SkillLimits | None = None) -> None:
        self._limits = limits or SkillLimits()
        self._records: dict[tuple[str, str], SkillRecord] = {}

    @property
    def limits(self) -> SkillLimits:
        return self._limits

    # -- registration ---------------------------------------------------

    def register(
        self,
        metadata: SkillMetadata,
        body: str,
        resources: Mapping[str, str] | None = None,
    ) -> SkillRecord:
        """Register one skill. Raises when it does not fit the declared budgets."""
        scope = normalize_scope(metadata.scope)
        if len(body) > self._limits.body_chars:
            raise SkillBudgetExceeded(
                f"skill {metadata.skill_id} body is {len(body)} chars, limit is {self._limits.body_chars}"
            )
        checked: dict[str, str] = {}
        resource_items = tuple((resources or {}).items())
        if len(resource_items) > self._limits.max_resources:
            raise SkillBudgetExceeded(
                f"skill {metadata.skill_id} declares {len(resource_items)} resources, "
                f"limit is {self._limits.max_resources}"
            )
        for raw_path, text in resource_items:
            try:
                path = safe_resource_path(raw_path)
            except SkillResourceDenied as exc:
                raise SkillResourceDenied(metadata.skill_id, raw_path, exc.reason) from exc
            if len(text.encode("utf-8")) > self._limits.resource_bytes:
                raise SkillBudgetExceeded(
                    f"skill {metadata.skill_id} resource {path} exceeds {self._limits.resource_bytes} bytes"
                )
            checked[path] = text
        record = SkillRecord(
            metadata=SkillMetadata(**{**metadata.__dict__, "scope": scope}),
            body=body,
            resources=checked,
        )
        self._records[(scope, metadata.skill_id)] = record
        return record

    def unregister(self, skill_id: str, *, scope: str | None = None) -> None:
        self._records.pop((normalize_scope(scope), skill_id), None)

    # -- port -----------------------------------------------------------

    def _resolve(self, skill_id: str, scope: str | None) -> SkillRecord:
        wanted = normalize_scope(scope)
        if wanted != "global" and (wanted, skill_id) in self._records:
            return self._records[(wanted, skill_id)]
        if ("global", skill_id) in self._records:
            return self._records[("global", skill_id)]
        raise SkillNotFound(skill_id, wanted)

    async def list_metadata(self, *, scope: str | None = None) -> Sequence[SkillMetadata]:
        wanted = normalize_scope(scope)
        visible: list[SkillMetadata] = []
        for (record_scope, _skill_id), record in sorted(self._records.items()):
            if record_scope in {"global", wanted}:
                visible.append(
                    SkillMetadata(**{**record.metadata.__dict__, "fingerprint": record.fingerprint()})
                )
        return tuple(visible)

    async def load_body(self, skill_id: str, *, scope: str | None = None) -> SkillBody:
        record = self._resolve(skill_id, scope)
        return SkillBody(
            skill_id=record.metadata.skill_id,
            version=record.metadata.version,
            text=record.body,
            fingerprint=record.fingerprint(),
            scope=record.metadata.scope,
            resources=tuple(sorted(record.resources)),
            metadata=dict(record.metadata.__dict__),
        )

    async def read_resource(
        self, skill_id: str, path: str, *, scope: str | None = None
    ) -> SkillResource:
        record = self._resolve(skill_id, scope)
        try:
            canonical = safe_resource_path(path)
        except SkillResourceDenied as exc:
            raise SkillResourceDenied(skill_id, path, exc.reason) from exc
        if canonical not in record.resources:
            raise SkillNotFound(f"{skill_id}:{canonical}", record.metadata.scope)
        text = record.resources[canonical]
        return SkillResource(
            skill_id=skill_id,
            path=canonical,
            text=text,
            fingerprint=fingerprint_text(text),
            byte_length=len(text.encode("utf-8")),
        )

    async def fingerprint(self, skill_id: str, *, scope: str | None = None) -> str:
        return self._resolve(skill_id, scope).fingerprint()

    # -- introspection --------------------------------------------------

    def registered_ids(self, *, scope: str | None = None) -> tuple[str, ...]:
        wanted = normalize_scope(scope)
        return tuple(
            sorted(
                skill_id
                for (record_scope, skill_id) in self._records
                if record_scope in {"global", wanted}
            )
        )

    def skills_for(self, scopes: Iterable[str]) -> tuple[str, ...]:
        """Ids visible from any of ``scopes`` (used by hosts that aggregate projects)."""
        wanted = {normalize_scope(scope) for scope in scopes} | {"global"}
        return tuple(
            sorted(skill_id for (record_scope, skill_id) in self._records if record_scope in wanted)
        )
