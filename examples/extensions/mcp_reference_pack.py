"""Reference MCP extension pack: two remote tools and one guidance skill.

The pack does not implement MCP. It declares the aliases the existing catalog
will freeze, binds them explicitly, and lets ``mcp_tool_factory`` own the one
manager/connection lifecycle. The host supplies the server configuration; the
pack never contains a credential, endpoint or authorization decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from yanxu_harness.extensions import (
    ContributionDeclarations,
    ExtensionBinding,
    ExtensionManifest,
    SkillContribution,
    plan_extensions,
)
from yanxu_harness.extensions.mcp import mcp_tool_factory
from yanxu_harness.integrations.mcp import McpToolBinding, local_alias
from yanxu_harness.skills.memory import InMemorySkillProvider
from yanxu_harness.skills.provider import SkillLimits, SkillMetadata
from yanxu_harness.tools.definition import ToolEffect

__all__ = [
    "MCP_REFERENCE_ID",
    "MCP_SKILL_ID",
    "McpReferencePack",
    "build_mcp_reference_pack",
]

MCP_REFERENCE_ID = "yanxu.mcp-reference-pack"
MCP_SKILL_ID = "mcp_guidance"


@dataclass(frozen=True)
class McpReferencePack:
    plan: Any
    factory: Any
    echo: str
    bump: str
    explode: str
    bindings: Mapping[str, McpToolBinding]


def build_mcp_reference_pack(
    *,
    config: Any,
    namespace: str = "project-a",
    authorization_scope: str = "authorized",
    config_revision: str = "cfg-1",
    policy_revision: str = "pol-1",
    factory: Any = None,
) -> McpReferencePack:
    """Build the plan. The caller opens it; importing/binding creates no connection."""
    echo = local_alias(config.server_id, "echo")
    bump = local_alias(config.server_id, "bump")
    explode = local_alias(config.server_id, "explode")
    bindings = {
        "echo": McpToolBinding(
            remote_name="echo",
            effects=frozenset({ToolEffect.READ}),
            result_char_limit=8000,
        ),
        "bump": McpToolBinding(
            remote_name="bump",
            effects=frozenset({ToolEffect.WRITE}),
            preview=lambda context, arguments: {
                "action": "increment",
                "remote_tool": "bump",
                "key": arguments.get("key"),
            },
            policy_revision=policy_revision,
        ),
        "explode": McpToolBinding(
            remote_name="explode",
            effects=frozenset({ToolEffect.READ}),
            result_char_limit=8000,
        ),
    }
    tools = (echo, bump, explode)

    provider = InMemorySkillProvider(SkillLimits())
    provider.register(
        SkillMetadata(
            skill_id=MCP_SKILL_ID,
            name="MCP Guidance",
            description="Use the frozen remote echo/bump tools through the one gateway.",
            version="1",
            when_to_use="A task asks for a value from, or a write to, the MCP fixture.",
            avoid_when="The task can be answered locally without the remote service.",
            tools=(echo, bump),
            scope=namespace,
        ),
        "Call the remote tool through the deployment gateway. "
        "A bump is a write and therefore requires an approval bound to its preview.",
        {"guide.txt": "Echo is read-only; bump is a previewed write."},
    )
    manifest = ExtensionManifest(
        id=MCP_REFERENCE_ID,
        version="1.0.0",
        requires_capabilities=("mcp.tools.v1", "skills.v1", "tools.v1"),
        contributes=ContributionDeclarations(tools=tools, skills=(MCP_SKILL_ID,)),
        title="MCP reference pack",
        description="Two remote tools plus one guidance skill over the pinned MCP adapter.",
    )
    binding = ExtensionBinding(
        extension_id=MCP_REFERENCE_ID,
        namespace=namespace,
        authorization_scope=authorization_scope,
        config_revision=config_revision,
        policy_revision=policy_revision,
        capabilities=frozenset({"mcp.tools.v1", "skills.v1", "tools.v1"}),
        tools=tools,
        skills=(MCP_SKILL_ID,),
    )
    skill = SkillContribution(provider=provider)
    if factory is None:
        factory = mcp_tool_factory(
            config=config,
            bindings=bindings,
            skills=(skill,),
        )
    plan = plan_extensions(
        manifests=[manifest],
        factories={MCP_REFERENCE_ID: factory},
        bindings=[binding],
    )
    return McpReferencePack(
        plan=plan,
        factory=factory,
        echo=echo,
        bump=bump,
        explode=explode,
        bindings=bindings,
    )
