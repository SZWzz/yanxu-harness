"""EXT-AC09–13/16: one atomic publication, conflicts, rollback and approvals."""

from __future__ import annotations

import asyncio
import json

import pytest

from yanxu_harness.extensions import (
    ExtensionConflict,
    ExtensionContribution,
    ExtensionLimitExceeded,
    ExtensionOpenFailed,
    ExtensionRefused,
    ExtensionState,
    open_extensions,
    plan_extensions,
    SkillContribution,
)
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
)
from yanxu_harness.tools.registry import ToolRegistry

from ext_support import (
    RecordingFactory,
    binding,
    manifest,
    read_tool,
    skill_provider,
    write_tool,
)


def context(namespace: str = "project-a") -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace=namespace, session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author"),
    )


def plan_for(contribution, *, tools=(), skills=("reference_lookup",), local=(), binding_overrides=None):
    kwargs = dict(binding_overrides or {})
    factory = RecordingFactory(contribution)
    return plan_extensions(
        manifests=[manifest(tools=tuple(tools), skills=tuple(skills))],
        factories={"example.reference": factory},
        bindings=[binding(tools=tuple(tools), skills=tuple(skills), **kwargs)],
        local_tools=local,
    ), factory


async def test_a_combined_tool_set_keeps_every_execution_semantic() -> None:
    host = read_tool("host.local.read")
    extension_tool = read_tool()
    plan, factory = plan_for(
        ExtensionContribution(tools=(extension_tool,)),
        tools=("example.reference.lookup",),
        skills=(),
        local=[host],
    )
    async with open_extensions(plan) as deployment:
        definition = deployment.registry.get("example.reference.lookup")
        assert definition.version.startswith("1+ext.")
        assert definition.description == extension_tool.description
        assert definition.effects == frozenset({ToolEffect.READ})
        assert definition.handler is extension_tool.handler
        assert definition.args_validator is extension_tool.args_validator
        assert json.dumps(definition.schema(), sort_keys=True) == json.dumps(
            extension_tool.schema(), sort_keys=True
        )
        assert deployment.registry.names() == ("example.reference.lookup", "host.local.read")
        assert deployment.state is ExtensionState.READY

        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(auto_write=True),
            approvals=InMemoryApprovalStore(),
            backend=RecordingExecutionBackend(),
        )
        result = await gateway.invoke(
            ToolInvocation(
                call_id="call-1",
                namespace="project-a",
                tool_name="example.reference.lookup",
                arguments={"term": "alpha"},
                run_id="r1",
            ),
            context(),
        )
        assert result.status is InvocationStatus.EXECUTED
        assert result.tool_version == definition.version


async def test_a_write_tool_still_previews_and_waits_for_approval() -> None:
    write = write_tool()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    plan, _factory = plan_for(
        ExtensionContribution(tools=(write,)),
        tools=(write.name,),
        skills=(),
    )
    async with open_extensions(plan) as deployment:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=backend,
        )
        invocation = ToolInvocation(
            call_id="call-write",
            namespace="project-a",
            tool_name=write.name,
            arguments={"term": "beta"},
            run_id="r1",
        )
        waiting = await gateway.invoke(invocation, context())
        assert waiting.status is InvocationStatus.AWAITING_APPROVAL
        assert waiting.preview == {"action": "append", "term": "beta"}
        assert backend.intents == []

        await approvals.decide(
            waiting.approval.binding.key(), ApprovalStatus.APPROVED, "author"
        )
        executed = await gateway.invoke(invocation, context())
        assert executed.status is InvocationStatus.EXECUTED
        assert len(backend.intents) == 1
        assert len(backend.receipts) == 1


async def test_a_contribution_the_host_did_not_bind_is_refused() -> None:
    extra = read_tool()
    plan, factory = plan_for(
        ExtensionContribution(tools=(extra,)),
        tools=(),
        skills=(),
    )
    with pytest.raises(ExtensionRefused, match="did not bind"):
        async with open_extensions(plan):
            pass
    assert factory.closed == 1


async def test_a_bound_contribution_must_be_supplied() -> None:
    plan, factory = plan_for(
        ExtensionContribution(tools=()),
        tools=("example.reference.lookup",),
        skills=(),
    )
    with pytest.raises(ExtensionRefused, match="did not contribute"):
        async with open_extensions(plan):
            pass
    assert factory.closed == 1


async def test_the_same_tool_name_cannot_be_published_twice() -> None:
    plan = plan_extensions(
        manifests=[manifest()],
        factories={
            "example.reference": RecordingFactory(
                ExtensionContribution(tools=(read_tool(), read_tool()))
            )
        },
        bindings=[binding()],
    )
    with pytest.raises(ExtensionConflict, match="more than once"):
        async with open_extensions(plan):
            pass


async def test_a_missing_dependency_or_cycle_rolls_the_whole_batch_back() -> None:
    missing = read_tool(requires=("host.absent",))
    plan, factory = plan_for(
        ExtensionContribution(tools=(missing,)),
        tools=(missing.name,),
        skills=(),
    )
    with pytest.raises(ExtensionRefused, match="absent"):
        async with open_extensions(plan):
            pass
    assert factory.closed == 1

    first = read_tool("example.reference.one", requires=("example.reference.two",))
    second = read_tool("example.reference.two", requires=("example.reference.one",))
    cycle_plan = plan_extensions(
        manifests=[
            manifest(
                tools=(first.name, second.name),
                skills=(),
                capabilities=("tools.v1",),
            )
        ],
        factories={
            "example.reference": RecordingFactory(
                ExtensionContribution(tools=(first, second))
            )
        },
        bindings=[
            binding(
                tools=(first.name, second.name),
                skills=(),
                capabilities=("tools.v1",),
            )
        ],
    )
    with pytest.raises(ExtensionRefused, match="cycle"):
        async with open_extensions(cycle_plan):
            pass


async def test_a_dynamic_contribution_conflict_is_refused_and_rolled_back() -> None:
    # The plan only knows the bound name; the factory returns a different one that
    # was never declared. Nothing from this batch may be published.
    returned = read_tool("example.reference.other")
    plan, factory = plan_for(
        ExtensionContribution(tools=(returned,)),
        tools=("example.reference.lookup",),
        skills=(),
    )
    with pytest.raises(ExtensionRefused, match="did not bind"):
        async with open_extensions(plan):
            pass
    assert factory.closed == 1


async def test_the_second_factory_failing_cleans_the_first_and_itself() -> None:
    first = RecordingFactory(
        ExtensionContribution(tools=(read_tool("example.reference.lookup"),)),
    )
    second = RecordingFactory(
        ExtensionContribution(tools=(read_tool("example.zother.read"),)),
        fail_on_open=True,
    )
    host = ToolRegistry([read_tool("host.local.read")])
    plan = plan_extensions(
        manifests=[
            manifest(skills=(), capabilities=("tools.v1",)),
            manifest(
                extension_id="example.zother",
                tools=("example.zother.read",),
                skills=(),
                capabilities=("tools.v1",),
            ),
        ],
        factories={"example.reference": first, "example.zother": second},
        bindings=[
            binding(skills=(), capabilities=("tools.v1",)),
            binding(
                extension_id="example.zother",
                tools=("example.zother.read",),
                skills=(),
                capabilities=("tools.v1",),
            ),
        ],
        local_tools=host.definitions(),
    )

    with pytest.raises(ExtensionOpenFailed, match="example.zother"):
        async with open_extensions(plan):
            pass

    assert first.opened == 1 and first.closed == 1 and first.resources == 0
    assert second.opened == 1 and second.closed == 1 and second.resources == 0
    assert host.names() == ("host.local.read",)


async def test_a_skill_contribution_is_validated_against_the_binding() -> None:
    provider = skill_provider(skill_id="reference_lookup")
    plan, factory = plan_for(
        ExtensionContribution(skills=(SkillContribution(provider=provider, skill_ids=("reference_lookup",)),)),
        tools=(),
        skills=("reference_lookup",),
    )
    async with open_extensions(plan) as deployment:
        assert deployment.skills.registered_ids() == ("reference_lookup",)

    unbound_provider = skill_provider(skill_id="reference_lookup")
    bad_plan, bad_factory = plan_for(
        ExtensionContribution(
            skills=(
                SkillContribution(
                    provider=unbound_provider,
                    skill_ids=("reference_lookup", "another_skill"),
                ),
            )
        ),
        tools=(),
        skills=("reference_lookup",),
    )
    with pytest.raises(ExtensionRefused, match="did not bind"):
        async with open_extensions(bad_plan):
            pass
    assert bad_factory.closed == 1


async def test_the_published_registry_is_frozen_and_the_deployment_is_one_snapshot() -> None:
    plan, _factory = plan_for(
        ExtensionContribution(tools=(read_tool(),)),
        tools=("example.reference.lookup",),
        skills=(),
    )
    async with open_extensions(plan) as deployment:
        assert not hasattr(deployment.registry, "register")
        schema = deployment.registry.get("example.reference.lookup").schema()
        assert schema is not None
        with pytest.raises(TypeError):
            schema["properties"]["term"]["type"] = "integer"  # type: ignore[index]
        assert deployment.report()["state"] == "ready"
