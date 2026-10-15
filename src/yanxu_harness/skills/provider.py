"""Skills layer 1 and 2: bounded metadata, then on-demand content.

A skill is a *method*: prose that tells the model how to approach something, plus
optional resources it may read. Two properties matter more than any feature:

* **A skill grants nothing.** It cannot authorize a tool, add a permission,
  widen a scope or execute a script. Nothing in this module can produce a
  permission, an approval or an effect. A skill's ``tools`` list is a hint about
  which tools the method expects to use; the deployment policy decides whether
  those tools are callable, and discovery decides whether the model may see them.
* **Only metadata is cheap.** Listing skills loads bounded metadata; a body or a
  resource is loaded on demand, inside an explicit budget, and every loaded byte
  has a fingerprint so a later resume can prove it is reading the same content.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable

from ..errors import HarnessError

__all__ = [
    "SkillBody",
    "SkillLimits",
    "SkillMetadata",
    "SkillProvider",
    "SkillResource",
    "normalize_scope",
    "safe_resource_path",
]


class SkillNotFound(HarnessError):
    code = "skill_not_found"

    def __init__(self, skill_id: str, scope: str | None = None) -> None:
        super().__init__(f"skill {skill_id!r} is not available" + (f" in scope {scope!r}" if scope else ""))
        self.skill_id = skill_id
        self.scope = scope


class SkillResourceDenied(HarnessError):
    code = "skill_resource_denied"

    def __init__(self, skill_id: str, path: str, reason: str) -> None:
        super().__init__(f"{skill_id}: resource {path!r} refused: {reason}")
        self.skill_id = skill_id
        self.path = path
        self.reason = reason


class SkillBudgetExceeded(HarnessError):
    code = "skill_budget_exceeded"


def normalize_scope(scope: str | None) -> str:
    return (scope or "global").strip() or "global"


def safe_resource_path(path: str) -> str:
    """Return a canonical relative resource path or raise :class:`SkillResourceDenied`.

    Refuses absolute paths, ``..`` traversal, backslashes, drive letters, empty
    segments and control characters. A skill resource is data, so escaping the
    skill directory would silently turn "read a template" into "read the host's
    filesystem".
    """
    if not path:
        raise SkillResourceDenied("", path, "empty path")
    if "\\" in path:
        raise SkillResourceDenied("", path, "backslashes are not path separators here")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise SkillResourceDenied("", path, "absolute paths are not allowed")
    if any(ord(char) < 32 for char in path):
        raise SkillResourceDenied("", path, "control characters are not allowed")
    normalized = posixpath.normpath(path)
    if normalized in {".", ".."} or normalized.startswith("../") or normalized.startswith("/"):
        raise SkillResourceDenied("", path, "path escapes the skill directory")
    return normalized


@dataclass(frozen=True)
class SkillLimits:
    """The budgets a provider must respect when it loads anything."""

    metadata_chars: int = 4000
    body_chars: int = 60000
    resource_bytes: int = 262144
    max_resources: int = 32

    def __post_init__(self) -> None:
        for name in ("metadata_chars", "body_chars", "resource_bytes", "max_resources"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class SkillMetadata:
    """What a catalog listing may contain. Deliberately small."""

    skill_id: str
    name: str
    description: str
    version: str = "1"
    when_to_use: str = ""
    avoid_when: str = ""
    #: Tools the *method* expects; a hint, never an authorization.
    tools: tuple[str, ...] = ()
    scope: str = "global"
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.skill_id or not self.name:
            raise ValueError("a skill needs an id and a name")
        object.__setattr__(self, "tools", tuple(self.tools))


@dataclass(frozen=True)
class SkillBody:
    """A loaded skill body plus the provenance a resume has to re-verify."""

    skill_id: str
    version: str
    text: str
    fingerprint: str
    scope: str = "global"
    resources: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", tuple(self.resources))


@dataclass(frozen=True)
class SkillResource:
    """One loaded resource, with the byte count and digest that were observed."""

    skill_id: str
    path: str
    text: str
    fingerprint: str
    byte_length: int


@runtime_checkable
class SkillProvider(Protocol):
    """Where skills come from. Implementations must enforce :class:`SkillLimits`."""

    @property
    def limits(self) -> SkillLimits:
        """The budgets this provider enforces."""

    async def list_metadata(self, *, scope: str | None = None) -> Sequence[SkillMetadata]:
        """Return bounded metadata for every skill visible in ``scope``."""

    async def load_body(self, skill_id: str, *, scope: str | None = None) -> SkillBody:
        """Load one skill's body on demand."""

    async def read_resource(self, skill_id: str, path: str, *, scope: str | None = None) -> SkillResource:
        """Load one resource of one skill on demand."""

    async def fingerprint(self, skill_id: str, *, scope: str | None = None) -> str:
        """Return the current content fingerprint of one skill."""


def fingerprint_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
