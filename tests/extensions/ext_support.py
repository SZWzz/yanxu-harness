"""Shared, deliberately small fixtures for the extension acceptance suite.

Nothing here is exported by the package: the tests construct the same public
objects a host would, so a helper that quietly bypasses a contract would show up
as a test that cannot be written.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

from yanxu_harness.extensions import (
    ExtensionBinding,
    ExtensionContribution,
    ExtensionLimits,
    ExtensionManifest,
)
from yanxu_harness.skills.memory import InMemorySkillProvider
from yanxu_harness.skills.provider import SkillLimits, SkillMetadata
from yanxu_harness.tools.definition import ToolDefinition
from yanxu_harness.tools.outcomes import ToolArgsInvalid


def manifest_payload(
    *,
    extension_id: str = "example.reference",
    version: str = "1.0.0",
    tools: tuple[str, ...] = ("example.reference.lookup",),
    skills: tuple[str, ...] = ("reference_lookup",),
    capabilities: tuple[str, ...] = ("skills.v1", "tools.v1"),
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "manifest_schema": 1,
        "extension_api": 1,
        "id": extension_id,
        "version": version,
        "requires_capabilities": list(capabilities),
        "contributes": {"tools": list(tools), "skills": list(skills)},
    }
    payload.update(overrides)
    return payload


def manifest(**kwargs: Any) -> ExtensionManifest:
    from yanxu_harness.extensions import parse_manifest

    return parse_manifest(manifest_payload(**kwargs))


def binding(
    *,
    extension_id: str = "example.reference",
    namespace: str = "project-a",
    authorization_scope: str = "authorized",
    capabilities: tuple[str, ...] = ("skills.v1", "tools.v1"),
    tools: tuple[str, ...] = ("example.reference.lookup",),
    skills: tuple[str, ...] = ("reference_lookup",),
    config_revision: str = "cfg-1",
    policy_revision: str = "pol-1",
    **overrides: Any,
) -> ExtensionBinding:
    return ExtensionBinding(
        extension_id=extension_id,
        namespace=namespace,
        authorization_scope=authorization_scope,
        capabilities=frozenset(capabilities),
        tools=tools,
        skills=skills,
        config_revision=config_revision,
        policy_revision=policy_revision,
        **overrides,
    )


def read_tool(
    name: str = "example.reference.lookup",
    *,
    version: str = "1",
    description: str = "Look up one term in the reference index.",
    schema: Mapping[str, Any] | None = None,
    requires: tuple[str, ...] = (),
    effects: frozenset[Any] | None = None,
) -> ToolDefinition:
    from yanxu_harness.tools.definition import ToolEffect

    def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        term = arguments.get("term")
        if not isinstance(term, str) or not term.strip():
            raise ToolArgsInvalid("term must be a non-empty string")
        return {"term": term.strip()}

    async def handler(context: Any, arguments: Mapping[str, Any]) -> str:
        return f"{arguments['term']}: reference"

    return ToolDefinition(
        name=name,
        description=description,
        version=version,
        effects=effects or frozenset({ToolEffect.READ}),
        args_validator=validate,
        handler=handler,
        args_schema=schema
        if schema is not None
        else {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
            "additionalProperties": False,
        },
        requires=requires,
    )


def skill_provider(
    *,
    skill_id: str = "reference_lookup",
    body: str = "Use the lookup tool with one term.",
    scope: str = "project-a",
) -> InMemorySkillProvider:
    provider = InMemorySkillProvider(SkillLimits())
    provider.register(
        SkillMetadata(
            skill_id=skill_id,
            name=skill_id.replace("_", " ").title(),
            description="A reference method.",
            version="1",
            scope=scope,
        ),
        body,
        {"guide.txt": "reference guide"},
    )
    return provider


@dataclass
class RecordingFactory:
    """A factory that records every call and can fail at a chosen point.

    ``contribution`` may be a callable so a test can build data only when the
    factory is actually opened (which is how the zero-I/O precheck is proved).
    """

    contribution: Any = None
    fail_on_open: bool = False
    fail_close: bool = False
    touches: list[str] = field(default_factory=list)
    opened: int = 0
    closed: int = 0
    resources: int = 0
    contribution_calls: int = 0

    async def open(self, context: Any) -> ExtensionContribution:
        self.opened += 1
        self.resources += 1
        self.touches.append(f"open:{context.extension_id}")
        if self.fail_on_open:
            # The factory acquired a resource and then failed. Its own close is
            # the only thing that can release it; the assembly also calls close
            # on the failing factory to prove the contract.
            raise RuntimeError("factory failed after acquiring a resource")
        value = self.contribution
        if callable(value):
            self.contribution_calls += 1
            value = value()
        return value if value is not None else ExtensionContribution()

    async def close(self) -> None:
        self.closed += 1
        self.resources = 0
        if self.fail_close:
            raise RuntimeError("close failed")


@dataclass
class CountingFactory(RecordingFactory):
    pass


def deploy_tool(term: str = "alpha") -> dict[str, Any]:
    return {"term": term}


async def wait_for(condition: Any, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached before the timeout")
        await asyncio.sleep(0.01)


def tiny_limits(**overrides: Any) -> ExtensionLimits:
    return ExtensionLimits(**overrides)
