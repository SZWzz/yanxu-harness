"""Reference extension pack: standard-library tools, a skill and a resource.

This is a real, runnable example of the E0–E3 contract, not a test fixture:

* the manifest is bounded data;
* the host binding names the namespace, revisions and the two contributions it
  grants;
* the factory reads an opaque workspace reference from the binding context, so
  a private path never appears in a manifest, report or fingerprint;
* the lookup tool is read-only, the write tool has a synchronous preview and the
  skill is loaded on demand.

Nothing here imports an optional extra or touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from yanxu_harness.extensions import (
    ContributionDeclarations,
    ExtensionBinding,
    ExtensionContext,
    ExtensionContribution,
    ExtensionManifest,
    SkillContribution,
    plan_extensions,
)
from yanxu_harness.skills.memory import InMemorySkillProvider
from yanxu_harness.skills.provider import SkillLimits, SkillMetadata
from yanxu_harness.tools.definition import ExecutionMode, ToolEffect
from yanxu_harness.tools.definition import ToolDefinition
from yanxu_harness.tools.outcomes import ToolArgsInvalid

__all__ = [
    "LOOKUP_TOOL",
    "REFERENCE_ID",
    "REFERENCE_SKILL_ID",
    "WRITE_TOOL",
    "ReferencePackFactory",
    "build_reference_plan",
]

REFERENCE_ID = "yanxu.reference-pack"
LOOKUP_TOOL = "reference.lookup"
WRITE_TOOL = "reference.write"
REFERENCE_SKILL_ID = "reference_lookup"

_ENTRIES = {
    "alpha": "Alpha is the first reference entry.",
    "beta": "Beta is the second reference entry.",
}


def _validator(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    term = arguments.get("term")
    if not isinstance(term, str) or not term.strip():
        raise ToolArgsInvalid("term must be a non-empty string")
    return {"term": term.strip().lower()}


def _lookup(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    term = arguments["term"]
    return {
        "content": [{"type": "text", "text": _ENTRIES.get(term, f"No entry for {term}.")}],
        "details": {"term": term},
    }


@dataclass
class ReferencePackFactory:
    """Host-selected trusted code. It owns no background task and no connection."""

    opened: int = 0
    closed: int = 0
    last_open: ExtensionContext | None = None
    written: list[dict[str, Any]] = field(default_factory=list)

    async def open(self, context: ExtensionContext) -> ExtensionContribution:
        self.opened += 1
        self.last_open = context
        workspace = Path(context.resource("workspace"))
        workspace.mkdir(parents=True, exist_ok=True)
        journal = workspace / "reference-writes.jsonl"

        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            term = arguments.get("term")
            if not isinstance(term, str) or not term.strip():
                raise ToolArgsInvalid("term must be a non-empty string")
            return {"term": term.strip().lower()}

        def write(context_value: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            record = {"term": arguments["term"], "attempt": context_value.identity.attempt_id}
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.written.append(record)
            return {
                "content": [{"type": "text", "text": f"Recorded {record['term']}."}],
                "details": {"journal": "reference-writes.jsonl"},
            }

        def preview(context_value: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            return {
                "action": "append",
                "target": "reference-writes.jsonl",
                "term": arguments["term"],
            }

        lookup = ToolDefinition(
            name=LOOKUP_TOOL,
            description="Look up one term in the small reference index.",
            version="1",
            effects=frozenset({ToolEffect.READ}),
            args_validator=_validator,
            handler=_lookup,
            args_schema={
                "type": "object",
                "properties": {"term": {"type": "string"}},
                "required": ["term"],
                "additionalProperties": False,
            },
        )
        write_tool = ToolDefinition(
            name=WRITE_TOOL,
            description="Append one entry to the reference journal.",
            version="1",
            effects=frozenset({ToolEffect.WRITE}),
            args_validator=validate,
            handler=write,
            preview=preview,
            execution_mode=ExecutionMode.SEQUENTIAL,
            args_schema={
                "type": "object",
                "properties": {"term": {"type": "string"}},
                "required": ["term"],
                "additionalProperties": False,
            },
        )

        provider = InMemorySkillProvider(SkillLimits())
        provider.register(
            SkillMetadata(
                skill_id=REFERENCE_SKILL_ID,
                name="Reference Lookup",
                description="Use the reference tools for a single term.",
                version="1",
                when_to_use="A task asks for one reference term.",
                avoid_when="The task needs a broad search.",
                tools=(LOOKUP_TOOL,),
                scope=context.namespace,
            ),
            "Call reference.lookup with one lowercase term. "
            "A write is a separate, previewed decision.",
            {"guide.txt": "Look up one term; do not invent an entry."},
        )
        return ExtensionContribution(
            tools=(lookup, write_tool),
            skills=(SkillContribution(provider=provider),),
        )

    async def close(self) -> None:
        self.closed += 1


def build_reference_plan(
    *,
    workspace: str | Path,
    namespace: str = "project-a",
    authorization_scope: str = "authorized",
    config_revision: str = "cfg-1",
    policy_revision: str = "pol-1",
    factory: ReferencePackFactory | None = None,
):
    """Build the reference pack's plan. The caller still decides to open it."""
    factory = factory or ReferencePackFactory()
    manifest = ExtensionManifest(
        id=REFERENCE_ID,
        version="1.0.0",
        requires_capabilities=("skills.v1", "tools.v1"),
        contributes=ContributionDeclarations(
            tools=(LOOKUP_TOOL, WRITE_TOOL),
            skills=(REFERENCE_SKILL_ID,),
        ),
        title="Reference pack",
        description="A bounded read/write tool pair and one on-demand skill.",
    )
    binding = ExtensionBinding(
        extension_id=REFERENCE_ID,
        namespace=namespace,
        authorization_scope=authorization_scope,
        config_revision=config_revision,
        policy_revision=policy_revision,
        capabilities=frozenset({"skills.v1", "tools.v1"}),
        tools=(LOOKUP_TOOL, WRITE_TOOL),
        skills=(REFERENCE_SKILL_ID,),
        resource_refs={"workspace": str(workspace)},
    )
    plan = plan_extensions(
        manifests=[manifest],
        factories={REFERENCE_ID: factory},
        bindings=[binding],
    )
    return plan
