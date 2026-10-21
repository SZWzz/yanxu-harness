"""Pure planning: manifest + binding + factory references, no resources opened.

This is the E0 contract in executable form. Everything here is validation and
canonical identity; the only attribute read from a host factory is its ``open``
method, and it is compared to ``None`` rather than called. Validation failures
name both sources of a conflict and the aggregate limit that was exceeded.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from ..errors import ContractError
from ..skills.provider import SkillLimits
from ..tools.definition import ToolDefinition
from ..tools.registry import ToolRegistry
from ..versioning import fingerprint
from .canonical import (
    binding_identity,
    definition_identity,
    freeze_json,
    manifest_identity,
)
from .errors import ExtensionConflict, ExtensionLimitExceeded, ExtensionRefused
from .manifest import parse_manifest
from .types import (
    AssemblyPlan,
    ExtensionBinding,
    ExtensionLimits,
    ExtensionManifest,
    PlannedExtension,
    SkillContribution,
)

__all__ = ["describe_plan", "plan_extensions"]


def plan_extensions(
    *,
    manifests: Sequence[ExtensionManifest | Mapping[str, Any] | str | bytes],
    factories: Mapping[str, Any],
    bindings: Sequence[ExtensionBinding],
    local_tools: Sequence[ToolDefinition] = (),
    host_skills: Sequence[SkillContribution] = (),
    limits: ExtensionLimits | None = None,
    skill_limits: SkillLimits | None = None,
) -> AssemblyPlan:
    """Validate a deployment plan without opening a factory or performing I/O."""
    resolved_limits = limits or ExtensionLimits()
    if not isinstance(resolved_limits, ExtensionLimits):
        raise ContractError("limits must be an ExtensionLimits instance")
    resolved_skill_limits = skill_limits or SkillLimits()
    if not isinstance(resolved_skill_limits, SkillLimits):
        raise ContractError("skill_limits must be a SkillLimits instance")
    if not isinstance(factories, Mapping):
        raise ContractError("factories must be a mapping from extension id to factory")

    parsed = tuple(
        item if isinstance(item, ExtensionManifest) else parse_manifest(item, limits=resolved_limits)
        for item in manifests
    )
    if len(parsed) > resolved_limits.max_extensions:
        raise ExtensionLimitExceeded(
            f"{len(parsed)} extensions exceed the aggregate extension limit "
            f"of {resolved_limits.max_extensions}"
        )

    by_id: dict[str, ExtensionManifest] = {}
    for item in parsed:
        if item.id in by_id:
            raise ExtensionConflict(
                f"duplicate extension id {item.id!r}; a deployment has exactly one "
                "version of an extension (this build does not choose between versions)"
            )
        by_id[item.id] = item

    binding_by_id: dict[str, ExtensionBinding] = {}
    for item in bindings:
        if not isinstance(item, ExtensionBinding):
            raise ContractError(
                f"bindings must be ExtensionBinding, got {type(item).__name__}"
            )
        if item.extension_id in binding_by_id:
            raise ExtensionConflict(f"duplicate binding for extension {item.extension_id!r}")
        if item.extension_id not in by_id:
            raise ExtensionRefused(
                f"binding names unknown extension {item.extension_id!r}; the host may not "
                "create an extension the plan does not contain"
            )
        binding_by_id[item.extension_id] = item
    missing_bindings = sorted(set(by_id) - set(binding_by_id))
    if missing_bindings:
        raise ExtensionRefused(
            f"no host binding for extension(s): {', '.join(missing_bindings)}"
        )

    entries: list[PlannedExtension] = []
    for extension_id in sorted(by_id):
        item = by_id[extension_id]
        factory = factories.get(extension_id)
        if factory is None:
            raise ExtensionRefused(f"no factory for extension {extension_id!r}")
        open_method = getattr(factory, "open", None)
        if not callable(open_method):
            raise ExtensionRefused(
                f"factory for extension {extension_id!r} must expose open(context)"
            )
        close_method = getattr(factory, "close", None)
        if close_method is not None and not callable(close_method):
            raise ExtensionRefused(
                f"factory for extension {extension_id!r} has a non-callable close"
            )
        entries.append(
            PlannedExtension(
                manifest=item,
                binding=binding_by_id[extension_id],
                factory=factory,
            )
        )

    scopes = {
        (entry.binding.namespace, entry.binding.authorization_scope, entry.binding.config_revision)
        for entry in entries
    }
    if len(scopes) > 1:
        raise ExtensionRefused(
            "one deployment has exactly one (namespace, authorization_scope, "
            "config_revision); split different scopes into separate deployments"
        )
    namespace = authorization_scope = config_revision = ""
    if scopes:
        namespace, authorization_scope, config_revision = next(iter(scopes))

    for entry in entries:
        _validate_binding(entry.manifest, entry.binding)

    local = tuple(local_tools)
    if any(not isinstance(item, ToolDefinition) for item in local):
        raise ContractError("local_tools must contain ToolDefinition objects")
    # Reuse the existing registry checks (duplicate names, cycles) on a private
    # copy: the host's registry is never touched.
    ToolRegistry(local)

    host = tuple(host_skills)
    if any(not isinstance(item, SkillContribution) for item in host):
        raise ContractError("host_skills must contain SkillContribution objects")

    _check_tool_conflicts(entries, local, namespace)
    _check_skill_conflicts(entries, host, namespace)
    _check_totals(entries, local, host, resolved_limits)

    plan_fingerprint = fingerprint(
        {
            "plan_version": 1,
            "extensions": [
                {
                    "manifest": manifest_identity(entry.manifest),
                    "binding": binding_identity(entry.binding),
                }
                for entry in entries
            ],
            "host": {
                "tools": [
                    _local_tool_identity(item) for item in sorted(local, key=lambda tool: tool.name)
                ],
                "skills": [
                    {
                        "scope": item.scope or namespace,
                        "skill_ids": list(item.skill_ids),
                    }
                    for item in sorted(host, key=lambda item: (item.scope, item.skill_ids))
                ],
            },
            "limits": resolved_limits.as_public(),
            "skill_limits": _skill_limits_identity(resolved_skill_limits),
        }
    )

    return AssemblyPlan(
        extensions=tuple(entries),
        local_tools=local,
        host_skills=host,
        limits=resolved_limits,
        skill_limits=resolved_skill_limits,
        fingerprint=plan_fingerprint,
        namespace=namespace,
        authorization_scope=authorization_scope,
        config_revision=config_revision,
    )


def _validate_binding(manifest: ExtensionManifest, binding: ExtensionBinding) -> None:
    required = set(manifest.requires_capabilities)
    granted = set(binding.capabilities)
    if granted != required:
        missing = sorted(required - granted)
        extra = sorted(granted - required)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"not required by the manifest: {', '.join(extra)}")
        raise ExtensionRefused(
            f"{manifest.id}: the binding's capabilities do not match the manifest "
            f"requirement ({'; '.join(detail)})"
        )
    if manifest.contributes.tools and "tools.v1" not in required:
        raise ExtensionRefused(
            f"{manifest.id}: the manifest declares tools but does not require tools.v1"
        )
    if manifest.contributes.skills and "skills.v1" not in required:
        raise ExtensionRefused(
            f"{manifest.id}: the manifest declares skills but does not require skills.v1"
        )
    if binding.tools and "tools.v1" not in granted:
        raise ExtensionRefused(
            f"{manifest.id}: the binding grants tools without the tools.v1 capability"
        )
    if binding.skills and "skills.v1" not in granted:
        raise ExtensionRefused(
            f"{manifest.id}: the binding grants skills without the skills.v1 capability"
        )
    unbound_tools = sorted(set(binding.tools) - set(manifest.contributes.tools))
    if unbound_tools:
        raise ExtensionRefused(
            f"{manifest.id}: binding names tool(s) the manifest did not declare: "
            f"{', '.join(unbound_tools)}"
        )
    unbound_skills = sorted(set(binding.skills) - set(manifest.contributes.skills))
    if unbound_skills:
        raise ExtensionRefused(
            f"{manifest.id}: binding names skill(s) the manifest did not declare: "
            f"{', '.join(unbound_skills)}"
        )


def _check_tool_conflicts(
    entries: Sequence[PlannedExtension],
    local: Sequence[ToolDefinition],
    namespace: str,
) -> None:
    owners: dict[str, str] = {}
    for definition in local:
        owners[definition.name] = "the host"
    for entry in entries:
        source = f"extension {entry.manifest.id}"
        for name in entry.binding.tools:
            previous = owners.get(name)
            if previous is not None:
                raise ExtensionConflict(
                    f"tool name {name!r} is claimed by {previous} and by {source}"
                )
            owners[name] = source

    known = set(owners)
    for definition in local:
        missing = sorted(set(definition.requires) - known)
        if missing:
            raise ExtensionRefused(
                f"host tool {definition.name!r} requires tool(s) that this deployment does "
                f"not provide: {', '.join(missing)}"
            )


def _check_skill_conflicts(
    entries: Sequence[PlannedExtension],
    host: Sequence[SkillContribution],
    namespace: str,
) -> None:
    owners: dict[tuple[str, str], str] = {}
    for entry in entries:
        source = f"extension {entry.manifest.id}"
        for skill_id in entry.binding.skills:
            key = (namespace, skill_id)
            if key in owners:
                raise ExtensionConflict(
                    f"skill {skill_id!r} in scope {namespace!r} is claimed by "
                    f"{owners[key]} and by {source}"
                )
            owners[key] = source
    for index, item in enumerate(host):
        scope = item.scope or namespace
        if scope != namespace:
            raise ExtensionRefused(
                f"host skill source {index} targets scope {scope!r}, but this deployment's "
                f"scope is {namespace!r}; it would be invisible to every run"
            )
        for skill_id in item.skill_ids:
            key = (scope, skill_id)
            if key in owners:
                raise ExtensionConflict(
                    f"skill {skill_id!r} in scope {scope!r} is claimed by "
                    f"{owners[key]} and by host skill source {index}"
                )
            owners[key] = f"host skill source {index}"


def _check_totals(
    entries: Sequence[PlannedExtension],
    local: Sequence[ToolDefinition],
    host: Sequence[SkillContribution],
    limits: ExtensionLimits,
) -> None:
    declared_tools = len(local) + sum(len(entry.binding.tools) for entry in entries)
    if declared_tools > limits.max_tools:
        raise ExtensionLimitExceeded(
            f"{declared_tools} tools exceed the aggregate tool limit of {limits.max_tools}"
        )
    declared_skills = sum(len(entry.binding.skills) for entry in entries) + sum(
        len(item.skill_ids) for item in host
    )
    if declared_skills > limits.max_skills:
        raise ExtensionLimitExceeded(
            f"{declared_skills} skills exceed the aggregate skill limit of {limits.max_skills}"
        )


def _local_tool_identity(definition: ToolDefinition) -> Mapping[str, Any]:
    schema: Any
    if callable(definition.args_schema):
        # A pure precheck must not run host code. The callable's result is frozen
        # during open, where the final deployment identity includes its content.
        schema = {"deferred": "callable"}
    elif definition.args_schema is None:
        schema = None
    else:
        schema = freeze_json(definition.args_schema, where=f"{definition.name} schema")
    return definition_identity(definition, schema=schema)


def _skill_limits_identity(limits: SkillLimits) -> Mapping[str, int]:
    return {
        "metadata_chars": limits.metadata_chars,
        "body_chars": limits.body_chars,
        "resource_bytes": limits.resource_bytes,
        "max_resources": limits.max_resources,
    }


def describe_plan(plan: AssemblyPlan) -> Mapping[str, Any]:
    """A bounded, public report about a plan. It opens nothing and reads no resource."""
    if not isinstance(plan, AssemblyPlan):
        raise ContractError("describe_plan expects an AssemblyPlan")
    report: dict[str, Any] = {
        "state": "planned",
        "fingerprint": plan.fingerprint,
        "scope": {
            "namespace": plan.namespace,
            "authorization_scope": plan.authorization_scope,
            "config_revision": plan.config_revision,
        },
        "limits": plan.limits.as_public(),
        "extensions": [
            {
                "id": entry.manifest.id,
                "version": entry.manifest.version,
                "manifest_schema": entry.manifest.manifest_schema,
                "extension_api": entry.manifest.extension_api,
                "capabilities": sorted(entry.manifest.requires_capabilities),
                "tools": sorted(entry.binding.tools),
                "skills": sorted(entry.binding.skills),
                "state": "planned",
            }
            for entry in plan.extensions
        ],
        "host": {
            "tools": sorted(definition.name for definition in plan.local_tools),
            "skill_sources": len(plan.host_skills),
            "skills": sorted(
                skill_id for item in plan.host_skills for skill_id in item.skill_ids
            ),
        },
    }
    report = freeze_json(report, where="plan report")
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > plan.limits.max_report_bytes:
        raise ExtensionLimitExceeded(
            f"the plan report exceeds max_report_bytes={plan.limits.max_report_bytes}; "
            "refusing to truncate a deployment description"
        )
    return report
