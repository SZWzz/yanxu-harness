"""The lightweight extension protocol and assembly layer.

The public split is intentional:

* a **manifest** is bounded data (`parse_manifest`),
* a **binding** is the host's explicit decision,
* a **factory** is host-selected trusted code,
* a **plan** is pure validation with no resources open,
* opening a plan publishes a frozen registry/skill view and records a deployment
  identity that a real run can resume against.

The package is standard-library only. The optional MCP helper lives in
``yanxu_harness.extensions.mcp`` and is imported on first attribute access, so
``import yanxu_harness.extensions`` never imports the MCP extra.
"""

from __future__ import annotations

from typing import Any

from .errors import (
    ExtensionConflict,
    ExtensionError,
    ExtensionInvalidated,
    ExtensionLimitExceeded,
    ExtensionOpenFailed,
    ExtensionOwnerMismatch,
    ExtensionRefused,
    ExtensionStateError,
    ManifestError,
)
from .manifest import parse_manifest
from .plan import describe_plan, plan_extensions
from .types import (
    EXTENSION_API_VERSIONS,
    KNOWN_CAPABILITIES,
    MANIFEST_SCHEMA_VERSIONS,
    AssemblyPlan,
    CloseReport,
    ContributionDeclarations,
    ExtensionBinding,
    ExtensionContext,
    ExtensionContribution,
    ExtensionFactory,
    ExtensionLimits,
    ExtensionManifest,
    ExtensionState,
    PlannedExtension,
    SkillContribution,
)

__all__ = [
    "EXTENSION_API_VERSIONS",
    "KNOWN_CAPABILITIES",
    "MANIFEST_SCHEMA_VERSIONS",
    "AssemblyPlan",
    "CloseReport",
    "ContributionDeclarations",
    "ExtensionBinding",
    "ExtensionConflict",
    "ExtensionContext",
    "ExtensionContribution",
    "ExtensionError",
    "ExtensionFactory",
    "ExtensionInvalidated",
    "ExtensionLimitExceeded",
    "ExtensionLimits",
    "ExtensionManifest",
    "ExtensionOpenFailed",
    "ExtensionOwnerMismatch",
    "ExtensionRefused",
    "ExtensionState",
    "ExtensionStateError",
    "ManifestError",
    "PlannedExtension",
    "SkillContribution",
    "describe_plan",
    "parse_manifest",
    "plan_extensions",
]


def __getattr__(name: str) -> Any:
    """Load the optional MCP helper (and the assembly layer) only when asked.

    ``open_extensions`` is implemented in ``assembly``; importing it eagerly is
    fine (it is standard-library only), but keeping it here makes the E0 package
    importable before E1 lands. The MCP helper is the one that must stay lazy.
    """
    if name == "open_extensions":
        from .assembly import open_extensions

        return open_extensions
    if name == "mcp_tool_factory":
        from .mcp import mcp_tool_factory

        return mcp_tool_factory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
