"""Contract checks for a published extension deployment.

A host that embeds the extension layer can call this from its own test suite;
it imports no pytest and opens no factory. The checks are deliberately about
observable contracts: a real validator, a preview for writes, a bounded report,
and the four skill interfaces with lazy content.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from ..extensions import ExtensionDeployment, ExtensionState
from ..extensions.errors import ExtensionInvalidated
from ..skills.provider import SkillProvider
from ..tools.definition import ToolDefinition
from .contract_suite import check_skill_provider_contract

__all__ = ["check_extension_deployment_contract"]


async def check_extension_deployment_contract(
    deployment: ExtensionDeployment,
    *,
    scope: str,
    skill_ids: Sequence[str] = (),
) -> list[str]:
    """Exercise the published deployment. Returns the names of the checks that passed.

    Raises :class:`AssertionError` on the first violation. It never invalidates or
    reopens the deployment, and it never asks a factory for anything.
    """
    assert isinstance(deployment, ExtensionDeployment), "expected an extension deployment"
    assert deployment.state is ExtensionState.READY, deployment.state
    observed: list[str] = []

    definitions = deployment.registry.definitions()
    for definition in definitions:
        assert isinstance(definition, ToolDefinition)
        assert definition.version, f"{definition.name}: an empty tool version"
        assert definition.executable, f"{definition.name}: a published tool has no handler"
        assert definition.args_validator is not None, f"{definition.name}: no validator"
        if definition.writes:
            assert definition.preview is not None, f"{definition.name}: a write has no preview"
    observed.append("tool-contracts")

    report = deployment.report()
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(raw.encode("utf-8")) <= deployment.plan.limits.max_report_bytes
    assert report["state"] == "ready"
    assert "resource_refs" not in raw and "credential" not in raw
    observed.append("report-bounded")

    provider: SkillProvider = deployment.skills
    metadata = await provider.list_metadata(scope=scope)
    assert len(metadata) <= deployment.plan.limits.max_skills
    observed.append("metadata-listed")

    for skill_id in skill_ids:
        checks = await check_skill_provider_contract(provider, skill_id=skill_id, scope=scope)
        assert "path-confined" in checks
        observed.append(f"skill:{skill_id}")

    if not skill_ids:
        observed.append("no-skills")
    return observed
