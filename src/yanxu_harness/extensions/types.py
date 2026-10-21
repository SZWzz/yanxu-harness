"""Public data types of the extension protocol.

The split is deliberate and is the main safety property of this layer:

* :class:`ExtensionManifest` is *data*: it says what an extension needs and
  declares the contributions it will provide.
* :class:`ExtensionBinding` is *the host's decision*: which namespace, which
  authorization/config/policy revision, which capabilities and which declared
  contributions are actually deployed.
* :class:`ExtensionFactory` is *host-selected executable code*: the manifest can
  never name a Python path, install a package or start a subprocess.
* :class:`ExtensionContribution` is what a factory actually opened. It is
  validated against the manifest and the binding; a contribution the host did
  not bind is refused rather than silently published.

None of these types creates a principal, an approval or a ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import ContractError
from ..skills.provider import SkillLimits, SkillMetadata, SkillProvider
from ..tools.definition import ToolDefinition
from .canonical import FrozenDict, freeze_json
from .errors import ExtensionRefused

__all__ = [
    "AssemblyPlan",
    "CloseReport",
    "ContributionDeclarations",
    "EXTENSION_API_VERSIONS",
    "ExtensionBinding",
    "ExtensionContext",
    "ExtensionContribution",
    "ExtensionFactory",
    "ExtensionLimits",
    "ExtensionManifest",
    "ExtensionState",
    "KNOWN_CAPABILITIES",
    "MANIFEST_SCHEMA_VERSIONS",
    "PlannedExtension",
    "SkillContribution",
]

#: Manifest schema and API generations this build can read. Adding a value is a
#: behavioural change and needs a test that reads a real document of that shape.
MANIFEST_SCHEMA_VERSIONS: tuple[int, ...] = (1,)
EXTENSION_API_VERSIONS: tuple[int, ...] = (1,)

#: Capabilities are explicit names, not a dependency solver. A capability this
#: build does not implement is refused instead of being treated as a no-op.
KNOWN_CAPABILITIES = frozenset({"tools.v1", "skills.v1", "mcp.tools.v1"})

#: Tool names follow the existing stable-id style (``ToolDefinition``).
TOOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")
#: Skill ids are an existing, slightly broader namespace; they are still bounded.
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
EXTENSION_ID_RE = TOOL_NAME_RE
_REVISION_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{name} must be a positive integer, got {value!r}")
    return value


def _positive_seconds(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ContractError(f"{name} must be a positive number of seconds, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class ExtensionLimits:
    """Bounded assembly limits. A host may raise them explicitly, never implicitly."""

    max_extensions: int = 32
    max_tools: int = 256
    max_skills: int = 256
    max_manifest_bytes: int = 65536
    max_manifest_depth: int = 16
    max_manifest_nodes: int = 4000
    max_report_bytes: int = 262144
    max_skill_metadata_chars: int = 64000
    startup_timeout_seconds: float = 30.0
    close_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "max_extensions",
            "max_tools",
            "max_skills",
            "max_manifest_bytes",
            "max_manifest_depth",
            "max_manifest_nodes",
            "max_report_bytes",
            "max_skill_metadata_chars",
        ):
            _positive_int(name, getattr(self, name))
        for name in ("startup_timeout_seconds", "close_timeout_seconds"):
            _positive_seconds(name, getattr(self, name))

    def as_public(self) -> Mapping[str, Any]:
        return {
            "max_extensions": self.max_extensions,
            "max_tools": self.max_tools,
            "max_skills": self.max_skills,
            "max_manifest_bytes": self.max_manifest_bytes,
            "max_manifest_depth": self.max_manifest_depth,
            "max_manifest_nodes": self.max_manifest_nodes,
            "max_report_bytes": self.max_report_bytes,
            "max_skill_metadata_chars": self.max_skill_metadata_chars,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "close_timeout_seconds": self.close_timeout_seconds,
        }


def _check_stable_id(value: str, *, what: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ContractError(
            f"{what} {value!r} is not a stable id (letters, digits, dot, dash, underscore; max 120)"
        )
    return value


def _check_string_tuple(values: Sequence[str], *, what: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ContractError(f"{what} must be a sequence of strings")
    checked: list[str] = []
    seen: set[str] = set()
    for value in values:
        _check_stable_id(value, what=what, pattern=pattern)
        if value in seen:
            raise ContractError(f"{what} lists {value!r} more than once")
        seen.add(value)
        checked.append(value)
    return tuple(checked)


def _check_revision(name: str, value: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.match(value):
        raise ContractError(f"{name} must be a non-empty, single-line revision string (max 120)")
    return value


@dataclass(frozen=True)
class ContributionDeclarations:
    """The declared names, in declaration order.

    Declaration order is preserved in the manifest identity (an array is ordered
    data), while name registration is conflict-checked and published in sorted
    order. Object key order and capability-set order never affect identity.
    """

    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tools", _check_string_tuple(self.tools, what="declared tool", pattern=TOOL_NAME_RE)
        )
        object.__setattr__(
            self, "skills", _check_string_tuple(self.skills, what="declared skill", pattern=SKILL_ID_RE)
        )


@dataclass(frozen=True)
class ExtensionManifest:
    """Versioned data. It never carries a permission, secret or Python path."""

    id: str
    version: str
    manifest_schema: int = 1
    extension_api: int = 1
    requires_capabilities: tuple[str, ...] = ()
    contributes: ContributionDeclarations = field(default_factory=ContributionDeclarations)
    title: str = ""
    description: str = ""
    homepage: str = ""

    def __post_init__(self) -> None:
        _check_stable_id(self.id, what="extension id", pattern=EXTENSION_ID_RE)
        if not isinstance(self.version, str) or not _VERSION_RE.match(self.version):
            raise ContractError(f"extension version {self.version!r} is not a bounded release identity")
        for name in ("manifest_schema", "extension_api"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractError(f"{name} must be an integer")
        supported = MANIFEST_SCHEMA_VERSIONS
        if self.manifest_schema not in supported:
            raise ContractError(
                f"manifest_schema {self.manifest_schema!r} is not supported (supported: {supported})"
            )
        if self.extension_api not in EXTENSION_API_VERSIONS:
            raise ContractError(
                f"extension_api {self.extension_api!r} is not supported "
                f"(supported: {EXTENSION_API_VERSIONS})"
            )
        capabilities = _check_string_tuple(
            self.requires_capabilities, what="required capability", pattern=_REVISION_RE
        )
        unknown = sorted(set(capabilities) - KNOWN_CAPABILITIES)
        if unknown:
            raise ContractError(f"unknown required capability: {', '.join(unknown)}")
        object.__setattr__(self, "requires_capabilities", tuple(sorted(set(capabilities))))
        if not isinstance(self.contributes, ContributionDeclarations):
            raise ContractError("contributes must be ContributionDeclarations")
        for name, limit in (("title", 200), ("homepage", 2048)):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > limit:
                raise ContractError(f"{name} must be a string of at most {limit} characters")
            if any(ord(char) < 32 for char in value):
                raise ContractError(f"{name} must be a single line")
        if not isinstance(self.description, str) or len(self.description) > 4000:
            raise ContractError("description must be a string of at most 4000 characters")
        if any(ord(char) < 32 and char not in "\n\t" for char in self.description):
            raise ContractError("description contains a control character")


@dataclass(frozen=True)
class ExtensionBinding:
    """What the host actually allows for one extension.

    A binding cannot create a capability, tool, skill or namespace that the
    manifest did not declare; it can only grant a subset (and at plan time the
    granted capabilities must exactly satisfy the manifest's requirements).
    ``resource_refs`` are opaque host references for the factory. They are never
    included in diagnostics, fingerprints or reports.
    """

    extension_id: str
    namespace: str
    capabilities: frozenset[str]
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    authorization_scope: str = "default"
    config_revision: str = "1"
    policy_revision: str = "1"
    resource_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_stable_id(self.extension_id, what="extension id", pattern=EXTENSION_ID_RE)
        _check_revision("namespace", self.namespace)
        _check_revision("authorization_scope", self.authorization_scope)
        _check_revision("config_revision", self.config_revision)
        _check_revision("policy_revision", self.policy_revision)
        if not isinstance(self.capabilities, (set, frozenset)):
            raise ContractError("capabilities must be a set or frozenset")
        capabilities = frozenset(self.capabilities)
        unknown = sorted(capabilities - KNOWN_CAPABILITIES)
        if unknown:
            raise ContractError(f"unknown capability in binding: {', '.join(unknown)}")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(
            self, "tools", _check_string_tuple(self.tools, what="bound tool", pattern=TOOL_NAME_RE)
        )
        object.__setattr__(
            self, "skills", _check_string_tuple(self.skills, what="bound skill", pattern=SKILL_ID_RE)
        )
        if not isinstance(self.resource_refs, Mapping):
            raise ContractError("resource_refs must be a mapping of opaque references")
        checked: dict[str, str] = {}
        for key, value in self.resource_refs.items():
            if not isinstance(key, str) or not TOOL_NAME_RE.match(key):
                raise ContractError(f"resource reference name {key!r} is not a stable id")
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ContractError(f"resource reference {key!r} must be a non-empty bounded string")
            if any(ord(char) < 32 for char in value):
                raise ContractError(f"resource reference {key!r} contains a control character")
            checked[key] = value
        object.__setattr__(self, "resource_refs", FrozenDict(checked))


@dataclass(frozen=True)
class ExtensionContext:
    """What a factory is allowed to see in :meth:`ExtensionFactory.open`.

    It carries the host's explicit references, namespace, revisions and the
    bound capability subset. It cannot mint a principal, widen a grant, write a
    ledger or create a second run context.
    """

    extension_id: str
    extension_version: str
    namespace: str
    authorization_scope: str
    config_revision: str
    policy_revision: str
    capabilities: frozenset[str]
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    resource_refs: Mapping[str, str] = field(default_factory=dict)
    limits: ExtensionLimits = field(default_factory=ExtensionLimits)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "skills", tuple(self.skills))
        object.__setattr__(self, "resource_refs", FrozenDict(dict(self.resource_refs)))

    def resource(self, name: str) -> str:
        """Read one host resource reference. Raises ``KeyError`` when absent."""
        return self.resource_refs[name]


@dataclass(frozen=True)
class SkillContribution:
    """A skill provider plus the ids it is allowed to expose in this deployment.

    ``skill_ids`` may be empty at construction for a host source whose listing is
    only known at open time; the assembly then freezes the ids returned for the
    bound scope. Extension contributions are validated against the binding.
    """

    provider: SkillProvider
    skill_ids: tuple[str, ...] = ()
    scope: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider, SkillProvider):
            raise ContractError(
                "a skill contribution needs a SkillProvider "
                "(list_metadata/load_body/read_resource/fingerprint/limits)"
            )
        object.__setattr__(
            self,
            "skill_ids",
            _check_string_tuple(self.skill_ids, what="contributed skill", pattern=SKILL_ID_RE),
        )
        if self.scope:
            _check_revision("skill scope", self.scope)


@dataclass(frozen=True)
class ExtensionContribution:
    """What one factory actually opened. Everything in it still needs validation."""

    tools: tuple[ToolDefinition, ...] = ()
    skills: tuple[SkillContribution, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.tools, (str, bytes)) or not isinstance(self.tools, Sequence):
            raise ContractError("contribution tools must be a sequence of ToolDefinition")
        for definition in self.tools:
            if not isinstance(definition, ToolDefinition):
                raise ContractError(
                    f"contribution tools must be ToolDefinition, got {type(definition).__name__}"
                )
        if not isinstance(self.skills, Sequence) or isinstance(self.skills, (str, bytes)):
            raise ContractError("contribution skills must be a sequence of SkillContribution")
        for item in self.skills:
            if not isinstance(item, SkillContribution):
                raise ContractError("contribution skills must contain SkillContribution objects")
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "skills", tuple(self.skills))


@runtime_checkable
class ExtensionFactory(Protocol):
    """Host-selected trusted code. This protocol is not a sandbox."""

    async def open(self, context: ExtensionContext) -> ExtensionContribution:
        """Acquire resources and return the contributions the host may publish."""

    async def close(self) -> None:
        """Release everything this factory acquired. Idempotent and bounded."""


@dataclass(frozen=True)
class PlannedExtension:
    """One manifest, its host binding and the factory the host selected."""

    manifest: ExtensionManifest
    binding: ExtensionBinding
    factory: ExtensionFactory


@dataclass(frozen=True)
class AssemblyPlan:
    """A validated, resource-free plan. Creating one starts no factory."""

    extensions: tuple[PlannedExtension, ...]
    local_tools: tuple[ToolDefinition, ...]
    host_skills: tuple[SkillContribution, ...]
    limits: ExtensionLimits
    skill_limits: SkillLimits
    fingerprint: str
    namespace: str = ""
    authorization_scope: str = ""
    config_revision: str = ""

    @property
    def state(self) -> str:
        return "planned"

    @property
    def extension_ids(self) -> tuple[str, ...]:
        return tuple(entry.manifest.id for entry in self.extensions)


class ExtensionState(str, Enum):
    """The lifecycle of one deployment."""

    NEW = "new"
    OPENING = "opening"
    READY = "ready"
    INVALIDATED = "invalidated"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"

    @property
    def usable(self) -> bool:
        return self is ExtensionState.READY


@dataclass(frozen=True)
class CloseReport:
    """What a close actually released, and what it cannot prove it released."""

    released: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()
    state: ExtensionState = ExtensionState.CLOSED

    @property
    def ok(self) -> bool:
        return not self.failed and not self.unconfirmed


def validate_contribution_type(value: Any) -> ExtensionContribution:
    """Accept only the declared contribution object, not a duck-typed mapping."""
    if not isinstance(value, ExtensionContribution):
        raise ExtensionRefused(
            "a factory must return ExtensionContribution; a mapping or bare tool is not a "
            "contribution and cannot be validated against the manifest"
        )
    return value


def public_metadata(metadata: SkillMetadata) -> Mapping[str, Any]:
    """A bounded, public projection of one skill's metadata."""
    return freeze_json(
        {
            "skill_id": metadata.skill_id,
            "name": metadata.name,
            "description": metadata.description[:4000],
            "version": metadata.version,
            "when_to_use": metadata.when_to_use[:4000],
            "avoid_when": metadata.avoid_when[:4000],
            "tools": list(metadata.tools),
            "scope": metadata.scope,
            "fingerprint": metadata.fingerprint,
        },
        where="skill metadata",
    )
