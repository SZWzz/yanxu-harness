"""EXT-AC21/23/26: revocation reaches the gateway, bridge, skills and harness."""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.errors import DeploymentRefused
from yanxu_harness.extensions import ExtensionInvalidated, ExtensionState
from yanxu_harness.runtime import RunRequest
from yanxu_harness.tools import (
    GatewayBindings,
    InvocationStatus,
    ToolInvocation,
    core_tool,
    result_status,
)
from yanxu_harness.tools.execution import EffectState

from ext_e2_deployment import (
    AUTHORIZATION_SCOPE,
    TOOL_NAME,
    ScriptedStream,
    build,
    final_message,
    manifest,
)


def invocation(call_id: str = "call-1", term: str = "alpha") -> ToolInvocation:
    return ToolInvocation(
        call_id=call_id,
        namespace="project-a",
        tool_name=TOOL_NAME,
        arguments={"term": term},
        run_id="r1",
    )


def bindings(bundle) -> GatewayBindings:
    return GatewayBindings(gateway=bundle.gateway, context=lambda: bundle.context)


async def test_revocation_refuses_gateway_and_bridge_calls_before_any_intent(tmp_path) -> None:
    calls: list[str] = []
    stream = ScriptedStream(final_message())
    bundle = await build(tmp_path, calls=calls, stream=stream)
    try:
        definition = bundle.deployment.registry.get(TOOL_NAME)
        bundle.deployment.invalidate("the host revoked this pack")
        assert bundle.deployment.state is ExtensionState.INVALIDATED

        direct = await bundle.gateway.invoke(invocation(), bundle.context)
        assert direct.status is InvocationStatus.DENIED
        assert "invalidated" in direct.reason

        spec = core_tool(definition, bindings(bundle))
        projected = await spec.execute("call-2", {"term": "beta"}, None, None)
        assert result_status(projected) is InvocationStatus.DENIED

        assert bundle.backend.all_operations() == (), "a revoked call leaves no ledger intent"
        assert calls == [], "a revoked call never reaches the handler"

        with pytest.raises(DeploymentRefused, match="invalidated|revoked"):
            await bundle.harness.start(RunRequest(prompt="write it"), bundle.context)
        assert stream.calls == 0, "a revoked deployment cannot ask the model anything"
    finally:
        bundle.close()


async def test_an_original_adapter_guard_is_still_enforced_and_not_overwritten(tmp_path) -> None:
    guard_calls: list[str] = []

    def catalog_guard(payload):
        guard_calls.append(str(payload.get("call_id")))
        return "the catalog moved" if payload.get("call_id") == "drift" else None

    bundle = await build(tmp_path, dispatch_guard=catalog_guard, auto_write=True)
    try:
        denied = await bundle.gateway.invoke(invocation("drift"), bundle.context)
        assert denied.status is InvocationStatus.DENIED
        assert "catalog moved" in denied.reason
        assert guard_calls == ["drift"]
        assert bundle.backend.all_operations() == ()

        bundle.deployment.invalidate("revoked")
        revoked = await bundle.gateway.invoke(invocation("ok-after-revocation"), bundle.context)
        assert revoked.status is InvocationStatus.DENIED
        assert "invalidated" in revoked.reason
        assert guard_calls == ["drift"], "the extension guard refuses before the adapter guard"
        assert bundle.backend.all_operations() == ()
    finally:
        bundle.close()


async def test_an_in_flight_call_keeps_its_unknown_receipt_after_revocation(tmp_path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(context, arguments):
        entered.set()
        await release.wait()
        raise RuntimeError("the remote write may have reached the server")

    bundle = await build(tmp_path, handler=handler, auto_write=True)
    try:
        task = asyncio.create_task(bundle.gateway.invoke(invocation(), bundle.context))
        await asyncio.wait_for(entered.wait(), timeout=5)
        bundle.deployment.invalidate("revoked while the call was in flight")
        release.set()
        result = await asyncio.wait_for(task, timeout=5)

        assert result.status is InvocationStatus.UNKNOWN
        assert result.receipt is not None
        assert result.receipt.state is EffectState.UNKNOWN
        operations = bundle.backend.all_operations()
        assert len(operations) == 1
        assert str(operations[0]["state"]) == "unknown"

        after = await bundle.gateway.invoke(
            invocation("call-after", term="beta"), bundle.context
        )
        assert after.status is InvocationStatus.DENIED
        assert len(bundle.backend.all_operations()) == 1, "no new intent after revocation"
    finally:
        bundle.close()


def test_the_deployment_digest_joins_the_compared_config_without_hiding_the_old_dimensions(tmp_path) -> None:
    async def scenario() -> None:
        first = await build(tmp_path / "one", version="1.0.0")
        try:
            baseline = dict(first.harness.fingerprints())
            assert set(baseline) >= {"config", "tools", "instructions", "policy", "deployment"}
            assert baseline["deployment"] == first.deployment.fingerprint

            first.harness._system_prompt = "different instructions"
            changed = dict(first.harness.fingerprints())
            assert changed["instructions"] != baseline["instructions"]
            assert changed["config"] != baseline["config"], (
                "the original config dimension still moves with the system prompt"
            )
        finally:
            first.close()

        second = await build(tmp_path / "two", version="2.0.0")
        try:
            upgraded = dict(second.harness.fingerprints())
            assert upgraded["deployment"] != baseline["deployment"]
            assert upgraded["config"] != baseline["config"], (
                "an extension version change is visible in the compared config"
            )
        finally:
            second.close()

    asyncio.run(scenario())


def test_manual_deployments_keep_their_exact_fingerprint_shape(tmp_path) -> None:
    from yanxu_harness.runtime import DefaultHarness
    from yanxu_harness.runtime.default_harness import request_fingerprint, tool_fingerprint
    from yanxu_harness.versioning import fingerprint

    harness = DefaultHarness(stream_fn=ScriptedStream(final_message()), system_prompt="be brief")
    values = harness.fingerprints()
    assert set(values) == {"config", "tools", "instructions", "policy"}
    assert values["config"] == fingerprint({"model": "", "system_prompt": "be brief"})
    assert values["tools"] == tool_fingerprint(())
    assert values["instructions"] == fingerprint("be brief")


async def test_revocation_invalidates_the_published_skill_view_too(tmp_path) -> None:
    from yanxu_harness.extensions import ExtensionContribution, SkillContribution, open_extensions, plan_extensions
    from ext_support import RecordingFactory, binding, manifest, skill_provider

    factory = RecordingFactory(
        ExtensionContribution(
            skills=(SkillContribution(provider=skill_provider()),)
        )
    )
    plan = plan_extensions(
        manifests=[
            manifest(
                tools=(),
                skills=("reference_lookup",),
                capabilities=("skills.v1",),
            )
        ],
        factories={"example.reference": factory},
        bindings=[
            binding(
                tools=(),
                skills=("reference_lookup",),
                capabilities=("skills.v1",),
            )
        ],
    )
    async with open_extensions(plan) as deployment:
        assert (await deployment.skills.list_metadata(scope="project-a"))[0].skill_id == "reference_lookup"
        deployment.invalidate("revoked")
        with pytest.raises(ExtensionInvalidated):
            await deployment.skills.list_metadata(scope="project-a")
        with pytest.raises(ExtensionInvalidated):
            await deployment.skills.load_body("reference_lookup", scope="project-a")
