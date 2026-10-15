"""A local ``SKILL.md`` provider: files on disk, bounded, confined.

The format is a markdown file with a YAML front matter block::

    ---
    id: outline
    name: Outline
    description: Turn a premise into an outline.
    version: 2
    when_to_use: The task is about structure.
    avoid_when: The task is about a single line.
    tools: [notes.read]
    ---
    How to build an outline…

Resources live in a ``resources/`` directory next to the file and are read on
demand. Three rules this provider exists to enforce:

* **Only the roots it was given are scanned.** No home directory, no disk walk,
  no implicit discovery: a skill that should not be visible must not be readable
  at all.
* **Everything is bounded and confined.** Metadata, body, resource size and
  resource count all come from :class:`~yanxu_harness.skills.provider.SkillLimits`,
  and every resource path goes through
  :func:`~yanxu_harness.skills.provider.safe_resource_path` so ``..`` cannot
  escape the skill directory.
* **YAML is parsed by a real parser, or not at all.** The ``yaml`` extra is
  optional; when it is missing the error says so and names the extra. A
  hand-written "good enough" YAML parser is how a skill ends up silently
  misconfigured.

Nothing here executes anything, grants anything or reads a file outside the
skill's own directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ConfigurationError, ContractError
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

__all__ = ["LocalSkillProvider", "SKILL_FILE", "RESOURCE_DIR"]

SKILL_FILE = "SKILL.md"
RESOURCE_DIR = "resources"


def _load_yaml() -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ConfigurationError(
            "reading SKILL.md front matter needs the optional 'yaml' extra: "
            "pip install 'yanxu-harness[yaml]'"
        ) from exc
    return yaml


@dataclass
class LocalSkillProvider:
    """A read-only provider over one or more explicitly given directories."""

    roots: Sequence[str | Path]
    limits: SkillLimits = SkillLimits()
    scope: str = "global"

    def __post_init__(self) -> None:
        self.roots = tuple(Path(root) for root in self.roots)
        self.scope = normalize_scope(self.scope)
        if not self.roots:
            raise ContractError("a local skill provider needs at least one root directory")

    # -- discovery ------------------------------------------------------

    def _skill_file(self, skill_id: str) -> Path:
        if not skill_id or os.sep in skill_id or "/" in skill_id or skill_id.startswith("."):
            raise SkillResourceDenied(skill_id, skill_id, "a skill id is a single directory name")
        for root in self.roots:
            candidate = root / skill_id / SKILL_FILE
            if candidate.is_file():
                return candidate
        raise SkillNotFound(skill_id, self.scope)

    @staticmethod
    def _split_front_matter(text: str, *, skill_id: str) -> tuple[Mapping[str, Any], str]:
        if not text.startswith("---"):
            raise ContractError(f"{skill_id}: {SKILL_FILE} must start with a '---' front matter block")
        parts = text.split("---", 2)
        if len(parts) < 3:
            raise ContractError(f"{skill_id}: front matter block is not closed")
        yaml = _load_yaml()
        try:
            front = yaml.safe_load(parts[1]) or {}
        except Exception as exc:  # noqa: BLE001 - the parser reports its own errors
            raise ContractError(f"{skill_id}: front matter is not valid YAML: {exc}") from exc
        if not isinstance(front, Mapping):
            raise ContractError(f"{skill_id}: front matter must be a mapping")
        return front, parts[2].lstrip("\n")

    @staticmethod
    def _metadata(skill_id: str, front: Mapping[str, Any], *, scope: str) -> SkillMetadata:
        tools = front.get("tools") or ()
        if isinstance(tools, str):
            tools = tuple(part.strip() for part in tools.split(",") if part.strip())
        return SkillMetadata(
            skill_id=str(front.get("id") or skill_id),
            name=str(front.get("name") or skill_id),
            description=str(front.get("description") or ""),
            version=str(front.get("version") or "1"),
            when_to_use=str(front.get("when_to_use") or ""),
            avoid_when=str(front.get("avoid_when") or ""),
            tools=tuple(str(item) for item in tools),
            scope=scope,
        )

    def _resources(self, skill_dir: Path) -> tuple[str, ...]:
        directory = skill_dir / RESOURCE_DIR
        if not directory.is_dir():
            return ()
        found: list[str] = []
        for path in sorted(directory.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                found.append(path.relative_to(directory).as_posix())
            if len(found) > self.limits.max_resources:
                raise SkillBudgetExceeded(
                    f"{skill_dir.name}: more than {self.limits.max_resources} resources"
                )
        return tuple(found)

    # -- port -----------------------------------------------------------

    async def list_metadata(self, *, scope: str | None = None) -> Sequence[SkillMetadata]:
        """Read only the front matter of each skill. Bodies are not loaded here."""
        wanted = normalize_scope(scope) if scope is not None else self.scope
        if wanted != self.scope:
            return ()
        visible: list[SkillMetadata] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
                skill_file = skill_dir / SKILL_FILE
                if not skill_file.is_file():
                    continue
                text = skill_file.read_text(encoding="utf-8")
                front, _body = self._split_front_matter(text, skill_id=skill_dir.name)
                metadata = self._metadata(skill_dir.name, front, scope=self.scope)
                visible.append(
                    SkillMetadata(
                        **{
                            **metadata.__dict__,
                            "fingerprint": self._fingerprint_of(skill_dir, metadata),
                        }
                    )
                )
        return tuple(visible)

    async def load_body(self, skill_id: str, *, scope: str | None = None) -> SkillBody:
        path = self._skill_file(skill_id)
        text = path.read_text(encoding="utf-8")
        front, body = self._split_front_matter(text, skill_id=skill_id)
        metadata = self._metadata(skill_id, front, scope=self.scope)
        if len(body) > self.limits.body_chars:
            raise SkillBudgetExceeded(
                f"{skill_id}: body is {len(body)} chars, limit is {self.limits.body_chars}"
            )
        return SkillBody(
            skill_id=metadata.skill_id,
            version=metadata.version,
            text=body,
            fingerprint=self._fingerprint_of(path.parent, metadata),
            scope=self.scope,
            resources=self._resources(path.parent),
            metadata=dict(metadata.__dict__),
        )

    async def read_resource(
        self, skill_id: str, path: str, *, scope: str | None = None
    ) -> SkillResource:
        skill_file = self._skill_file(skill_id)
        try:
            canonical = safe_resource_path(path)
        except SkillResourceDenied as exc:
            raise SkillResourceDenied(skill_id, path, exc.reason) from exc
        directory = (skill_file.parent / RESOURCE_DIR).resolve()
        target = (directory / canonical).resolve()
        if directory not in target.parents and target != directory:
            raise SkillResourceDenied(skill_id, path, "escapes the skill's resources directory")
        if not target.is_file():
            raise SkillNotFound(f"{skill_id}:{canonical}", self.scope)
        raw = target.read_bytes()
        if len(raw) > self.limits.resource_bytes:
            raise SkillBudgetExceeded(
                f"{skill_id}: resource {canonical} is {len(raw)} bytes, "
                f"limit is {self.limits.resource_bytes}"
            )
        text = raw.decode("utf-8", errors="strict")
        return SkillResource(
            skill_id=skill_id,
            path=canonical,
            text=text,
            fingerprint=fingerprint_text(text),
            byte_length=len(raw),
        )

    async def fingerprint(self, skill_id: str, *, scope: str | None = None) -> str:
        path = self._skill_file(skill_id)
        text = path.read_text(encoding="utf-8")
        front, _body = self._split_front_matter(text, skill_id=skill_id)
        return self._fingerprint_of(path.parent, self._metadata(skill_id, front, scope=self.scope))

    # -- internals ------------------------------------------------------

    def _fingerprint_of(self, skill_dir: Path, metadata: SkillMetadata) -> str:
        """Fingerprint the *content*, not the filesystem: body plus resource digests."""
        parts = [metadata.version]
        skill_file = skill_dir / SKILL_FILE
        if skill_file.is_file():
            parts.append(skill_file.read_text(encoding="utf-8"))
        for relative in self._resources(skill_dir):
            resource = skill_dir / RESOURCE_DIR / relative
            parts.append(f"{relative}\0{resource.read_text(encoding='utf-8')}")
        return fingerprint_text("\0".join(parts))
