"""EXT-AC14/15: the composite skill view is bounded, lazy, scoped and revocable."""

from __future__ import annotations

import pytest

from yanxu_harness.extensions import (
    ExtensionContribution,
    ExtensionInvalidated,
    ExtensionLimitExceeded,
    ExtensionLimits,
    ExtensionState,
    SkillContribution,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.skills.provider import SkillNotFound, SkillResourceDenied
from yanxu_harness.skills.memory import InMemorySkillProvider
from yanxu_harness.skills.provider import SkillLimits, SkillMetadata
from yanxu_harness.testing import check_skill_provider_contract

from ext_support import (
    CountingSkillProvider,
    RecordingFactory,
    binding,
    manifest,
    skill_provider,
)


def _plan(skill_provider_value, *, host_skills=(), limits=None):
    factory = RecordingFactory(
        ExtensionContribution(skills=(SkillContribution(provider=skill_provider_value),))
    )
    plan = plan_extensions(
        manifests=[manifest(tools=(), skills=("reference_lookup",))],
        factories={"example.reference": factory},
        bindings=[binding(tools=(), skills=("reference_lookup",))],
        host_skills=host_skills,
        limits=limits,
    )
    return plan, factory


async def test_the_composite_passes_the_existing_skill_provider_contract() -> None:
    plan, _factory = _plan(skill_provider())
    async with open_extensions(plan) as deployment:
        observed = await check_skill_provider_contract(
            deployment.skills, skill_id="reference_lookup", scope="project-a"
        )
        assert "metadata-bounded" in observed
        assert "body-on-demand" in observed
        assert "path-confined" in observed
        assert "fingerprint" in observed


async def test_metadata_is_listed_once_and_bodies_and_resources_stay_lazy() -> None:
    counting = CountingSkillProvider(skill_provider())
    plan, _factory = _plan(counting)
    async with open_extensions(plan) as deployment:
        assert counting.list_calls == 1
        assert counting.body_calls == 0
        assert counting.resource_calls == 0

        metadata = await deployment.skills.list_metadata(scope="project-a")
        assert [item.skill_id for item in metadata] == ["reference_lookup"]
        assert counting.body_calls == 0
        assert counting.resource_calls == 0

        body = await deployment.skills.load_body("reference_lookup", scope="project-a")
        assert body.text
        assert counting.body_calls == 1

        resource = await deployment.skills.read_resource(
            "reference_lookup", "guide.txt", scope="project-a"
        )
        assert resource.text == "reference guide"
        assert counting.resource_calls == 1


async def test_metadata_budget_is_checked_at_publication() -> None:
    plan, factory = _plan(
        skill_provider(),
        limits=ExtensionLimits(max_skill_metadata_chars=80),
    )
    with pytest.raises(ExtensionLimitExceeded, match="metadata"):
        async with open_extensions(plan):
            pass
    assert factory.closed == 1


async def test_resource_paths_are_confined() -> None:
    plan, _factory = _plan(skill_provider())
    async with open_extensions(plan) as deployment:
        with pytest.raises((SkillResourceDenied, SkillNotFound)):
            await deployment.skills.read_resource(
                "reference_lookup", "../secrets.txt", scope="project-a"
            )


async def test_a_global_or_other_scope_does_not_leak_across_namespaces() -> None:
    plan, _factory = _plan(skill_provider())
    async with open_extensions(plan) as deployment:
        assert await deployment.skills.list_metadata(scope="project-b") == ()
        assert await deployment.skills.list_metadata(scope="global") == ()
        with pytest.raises(SkillNotFound):
            await deployment.skills.load_body("reference_lookup", scope="project-b")
        with pytest.raises(SkillNotFound):
            await deployment.skills.fingerprint("reference_lookup", scope="project-b")


async def test_a_changed_source_invalidates_the_deployment_and_refuses_the_read() -> None:
    source = skill_provider()
    plan, _factory = _plan(source)
    async with open_extensions(plan) as deployment:
        frozen = await deployment.skills.fingerprint("reference_lookup", scope="project-a")
        source.register(
            SkillMetadata(
                skill_id="reference_lookup",
                name="Reference Lookup",
                description="A reference method.",
                version="2",
                scope="project-a",
            ),
            "Different method text.",
            {"guide.txt": "different guide"},
        )

        with pytest.raises(ExtensionInvalidated, match="fingerprint"):
            await deployment.skills.load_body("reference_lookup", scope="project-a")
        assert deployment.state is ExtensionState.INVALIDATED
        with pytest.raises(ExtensionInvalidated):
            await deployment.skills.list_metadata(scope="project-a")
        with pytest.raises(ExtensionInvalidated):
            await deployment.skills.fingerprint("reference_lookup", scope="project-a")
        assert frozen != ""


async def test_a_host_skill_source_collides_at_open_instead_of_overwriting() -> None:
    extension_source = skill_provider()
    host_source = skill_provider()
    plan, factory = _plan(
        extension_source,
        host_skills=(SkillContribution(provider=host_source, scope="project-a"),),
    )
    with pytest.raises(Exception, match="reference_lookup"):
        async with open_extensions(plan):
            pass
    assert factory.closed == 1


async def test_skill_metadata_cannot_register_a_tool_or_create_an_approval() -> None:
    provider = InMemorySkillProvider(SkillLimits())
    provider.register(
        SkillMetadata(
            skill_id="reference_lookup",
            name="Reference Lookup",
            description="A method.",
            version="1",
            tools=("example.reference.lookup",),
            scope="project-a",
        ),
        "Use the lookup tool.",
    )
    plan, _factory = _plan(provider)
    async with open_extensions(plan) as deployment:
        metadata = await deployment.skills.list_metadata(scope="project-a")
        assert metadata[0].tools == ("example.reference.lookup",)
        assert deployment.registry.names() == ()
