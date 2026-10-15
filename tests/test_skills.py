"""Skills: bounded metadata, scope isolation, confinement and budgets."""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.skills import (
    InMemorySkillProvider,
    SkillBudgetExceeded,
    SkillLimits,
    SkillMetadata,
    SkillNotFound,
    SkillResourceDenied,
    normalize_scope,
    safe_resource_path,
)
from yanxu_harness.testing import check_skill_provider_contract


def metadata(skill_id: str, scope: str = "global", version: str = "1") -> SkillMetadata:
    return SkillMetadata(
        skill_id=skill_id,
        name=skill_id.replace(".", " ").title(),
        description=f"{skill_id} method",
        version=version,
        when_to_use="when the task matches",
        avoid_when="when it does not",
        tools=("notes.read",),
        scope=scope,
    )


def build_provider() -> InMemorySkillProvider:
    provider = InMemorySkillProvider()
    provider.register(
        metadata("outline"),
        "How to build an outline.",
        {"templates/basic.md": "# Outline\n"},
    )
    provider.register(metadata("project.local", scope="project-a"), "Project A only.")
    return provider


def run(coro):
    return asyncio.run(coro)


def test_listing_returns_metadata_only_and_is_scoped() -> None:
    async def scenario() -> None:
        provider = build_provider()
        global_ids = [item.skill_id for item in await provider.list_metadata(scope="project-b")]
        assert global_ids == ["outline"]
        both = [item.skill_id for item in await provider.list_metadata(scope="project-a")]
        assert both == ["outline", "project.local"]

    run(scenario())


def test_a_skill_body_is_loaded_on_demand_with_a_fingerprint() -> None:
    async def scenario() -> None:
        provider = build_provider()
        body = await provider.load_body("outline")
        assert body.text.startswith("How to build")
        assert body.fingerprint.startswith("sha256:")
        assert body.resources == ("templates/basic.md",)

    run(scenario())


def test_a_body_change_changes_the_fingerprint() -> None:
    async def scenario() -> None:
        provider = build_provider()
        before = await provider.fingerprint("outline")
        provider.register(metadata("outline", version="2"), "A different outline method.")
        after = await provider.fingerprint("outline")
        assert before != after

    run(scenario())


def test_an_unknown_skill_is_reported_not_invented() -> None:
    async def scenario() -> None:
        provider = build_provider()
        with pytest.raises(SkillNotFound):
            await provider.load_body("does.not.exist")
        with pytest.raises(SkillNotFound):
            await provider.load_body("project.local", scope="project-b")

    run(scenario())


def test_a_project_skill_is_invisible_from_another_scope() -> None:
    async def scenario() -> None:
        provider = build_provider()
        with pytest.raises(SkillNotFound):
            await provider.load_body("project.local", scope="project-b")
        body = await provider.load_body("project.local", scope="project-a")
        assert body.scope == "project-a"

    run(scenario())


@pytest.mark.parametrize(
    "path",
    ["../outside.md", "/etc/passwd", "a/../../b", "", "dir\\file.md", "with\x00null.md", "C:/x.md"],
)
def test_an_escaping_resource_path_is_refused(path: str) -> None:
    with pytest.raises(SkillResourceDenied):
        safe_resource_path(path)


def test_a_legitimate_nested_path_is_normalised() -> None:
    assert safe_resource_path("templates/./basic.md") == "templates/basic.md"


def test_an_unknown_resource_is_refused() -> None:
    async def scenario() -> None:
        provider = build_provider()
        with pytest.raises(SkillNotFound):
            await provider.read_resource("outline", "templates/missing.md")
        resource = await provider.read_resource("outline", "templates/basic.md")
        assert resource.byte_length == len(resource.text.encode("utf-8"))
        assert resource.fingerprint.startswith("sha256:")

    run(scenario())


def test_a_body_over_the_budget_is_refused_at_registration() -> None:
    provider = InMemorySkillProvider(SkillLimits(body_chars=16))
    with pytest.raises(SkillBudgetExceeded, match="body"):
        provider.register(metadata("too.long"), "x" * 17)


def test_too_many_resources_are_refused() -> None:
    provider = InMemorySkillProvider(SkillLimits(max_resources=1))
    with pytest.raises(SkillBudgetExceeded, match="resources"):
        provider.register(metadata("many"), "body", {"a.md": "a", "b.md": "b"})


def test_an_oversized_resource_is_refused() -> None:
    provider = InMemorySkillProvider(SkillLimits(resource_bytes=4))
    with pytest.raises(SkillBudgetExceeded, match="exceeds"):
        provider.register(metadata("big"), "body", {"a.md": "12345"})


def test_a_skill_grants_no_permission_of_any_kind() -> None:
    """The metadata carries a tool *hint*; nothing in this API can grant authority."""
    async def scenario() -> None:
        provider = build_provider()
        listing = await provider.list_metadata()
        record = listing[0]
        # The only tool-shaped field is a plain tuple of names, and there is no
        # field anywhere on the metadata that expresses a permission or an approval.
        assert record.tools == ("notes.read",)
        assert not hasattr(record, "permissions")
        assert not hasattr(record, "approval")
        assert not hasattr(record, "execute")

    run(scenario())


def test_scope_normalisation_defaults_to_global() -> None:
    assert normalize_scope(None) == "global"
    assert normalize_scope("  ") == "global"
    assert normalize_scope(" project-a ") == "project-a"


def test_the_in_memory_provider_passes_the_shared_adapter_contract() -> None:
    async def scenario() -> None:
        provider = build_provider()
        observed = await check_skill_provider_contract(provider, skill_id="outline", scope="project-a")
        assert "path-confined" in observed
        assert "body-on-demand" in observed

    run(scenario())
