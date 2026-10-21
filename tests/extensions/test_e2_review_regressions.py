"""EXT-R1–R6: regressions found by the independent E0–E3 review."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from yanxu_harness.errors import HarnessError, ResumeRefused
from yanxu_harness.extensions import (
    ExtensionContribution,
    ExtensionOpenFailed,
    ExtensionRefused,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.runtime import Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolGateway,
    ToolInvocation,
    core_tool,
    result_status,
)

from ext_support import RecordingFactory, binding, manifest, read_tool, write_tool


def _context(
    namespace: str = "project-a",
    *,
    scope: str | None = "authorized",
    attempt: str = "a1",
) -> RunContext:
    labels = {} if scope is None else {"authorization_scope": scope}
    return RunContext(
        identity=RunIdentity(
            namespace=namespace, session_id="s", run_id="r", attempt_id=attempt
        ),
        principal=Principal(subject="review"),
        labels=labels,
    )


def _invocation(
    name: str,
    *,
    namespace: str = "project-a",
    call_id: str = "c",
    arguments: dict | None = None,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name=name,
        call_id=call_id,
        namespace=namespace,
        run_id="r",
        arguments={"term": "alpha"} if arguments is None else arguments,
    )


def _plan(factory, *, local=()):
    return plan_extensions(
        manifests=[manifest(tools=("example.reference.write",), skills=())],
        factories={"example.reference": factory},
        bindings=[binding(tools=("example.reference.write",), skills=())],
        local_tools=local,
    )


def _write_factory(effects: list[str]) -> RecordingFactory:
    async def handler(context, arguments):
        effects.append(str(arguments["term"]))
        return "wrote"

    return RecordingFactory(
        ExtensionContribution(tools=(replace(write_tool(), handler=handler),))
    )


class _BlockingGet(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, key):
        if self.block:
            self.entered.set()
            await self.release.wait()
        return await super().get(key)


class _BlockingPut(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def put(self, approval):
        self.entered.set()
        await self.release.wait()
        return await super().put(approval)


async def test_a_revocation_during_an_approved_read_cannot_admit(tmp_path) -> None:
    effects: list[str] = []
    deployment = await open_extensions(_plan(_write_factory(effects))).open()
    approvals = _BlockingGet()
    backend = RecordingExecutionBackend()
    try:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=backend,
        )
        pending = await gateway.invoke(_invocation("example.reference.write"), _context())
        assert pending.status is InvocationStatus.AWAITING_APPROVAL
        await approvals.decide(
            pending.approval.binding.key(), ApprovalStatus.APPROVED, "operator"
        )

        approvals.block = True
        task = asyncio.create_task(
            gateway.invoke(_invocation("example.reference.write", call_id="c2"), _context())
        )
        await asyncio.wait_for(approvals.entered.wait(), timeout=5)
        await deployment.aclose()
        approvals.release.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.status is InvocationStatus.DENIED, result.reason
        assert backend.intents == []
        assert effects == []
    finally:
        approvals.release.set()
        await deployment.aclose()


async def test_a_revocation_during_a_new_approval_write_cannot_admit(tmp_path) -> None:
    effects: list[str] = []
    deployment = await open_extensions(_plan(_write_factory(effects))).open()
    approvals = _BlockingPut()
    backend = RecordingExecutionBackend()
    task = None
    try:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=backend,
        )
        task = asyncio.create_task(
            gateway.invoke(_invocation("example.reference.write"), _context())
        )
        await asyncio.wait_for(approvals.entered.wait(), timeout=5)
        await deployment.aclose()
        approvals.release.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.status is InvocationStatus.DENIED, result.reason
        assert backend.intents == []
        assert effects == []
    finally:
        approvals.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await deployment.aclose()


async def test_a_direct_gateway_call_must_match_the_deployment_scope(tmp_path) -> None:
    effects: list[str] = []
    deployment = await open_extensions(_plan(_write_factory(effects))).open()
    backend = RecordingExecutionBackend()
    try:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(auto_write=True),
            approvals=InMemoryApprovalStore(),
            backend=backend,
        )
        wrong_namespace = await gateway.invoke(
            _invocation("example.reference.write", namespace="project-b", call_id="n"),
            _context("project-b"),
        )
        wrong_scope = await gateway.invoke(
            _invocation("example.reference.write", call_id="s"),
            _context(scope="unauthorized-b"),
        )
        missing_scope = await gateway.invoke(
            _invocation("example.reference.write", call_id="m"),
            _context(scope=None),
        )
        assert wrong_namespace.status is InvocationStatus.DENIED
        assert wrong_scope.status is InvocationStatus.DENIED
        assert missing_scope.status is InvocationStatus.DENIED
        assert backend.intents == [] and effects == []

        accepted = await gateway.invoke(
            _invocation("example.reference.write", call_id="ok"), _context()
        )
        assert accepted.status is InvocationStatus.EXECUTED, accepted.reason
        assert effects == ["alpha"]
        assert len(backend.intents) == 1
    finally:
        await deployment.aclose()


async def test_a_published_local_tool_is_revoked_with_its_deployment(tmp_path) -> None:
    effects: list[str] = []

    async def handler(context, arguments):
        effects.append(str(arguments["term"]))
        return "read"

    local = replace(read_tool("host.read"), handler=handler)
    deployment = await open_extensions(
        _plan(_write_factory(effects), local=(local,))
    ).open()
    backend = RecordingExecutionBackend()
    try:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(),
            approvals=InMemoryApprovalStore(),
            backend=backend,
        )
        definition = deployment.registry.get("host.read")
        await deployment.aclose()

        direct = await gateway.invoke(_invocation("host.read"), _context())
        assert direct.status is InvocationStatus.DENIED
        spec = core_tool(
            definition,
            GatewayBindings(gateway=gateway, context=lambda: _context()),
        )
        bridged = await spec.execute("call-2", {"term": "beta"}, None, None)
        assert result_status(bridged) is InvocationStatus.DENIED
        assert backend.intents == [] and effects == []
    finally:
        await deployment.aclose()


class _CleanupFactory:
    def __init__(
        self,
        *,
        fail_open: bool = False,
        close_mode: str = "ok",
        marker: str = "review-private-token-7f43",
    ) -> None:
        self.fail_open = fail_open
        self.close_mode = close_mode
        self.marker = marker
        self.resources = 0

    async def open(self, context):
        self.resources = 1
        if self.fail_open:
            raise RuntimeError(self.marker)
        return ExtensionContribution(tools=(write_tool(),))

    async def close(self):
        if self.close_mode == "raise":
            raise RuntimeError(self.marker)
        if self.close_mode == "timeout":
            await asyncio.sleep(10)
        self.resources = 0


class _NoCloseFactory(_CleanupFactory):
    close = None  # type: ignore[assignment]


def _single(factory, **kwargs):
    return plan_extensions(
        manifests=[manifest(tools=("example.reference.write",), skills=())],
        factories={"example.reference": factory},
        bindings=[binding(tools=("example.reference.write",), skills=())],
        **kwargs,
    )


async def test_a_failed_startup_close_failure_is_in_the_public_report() -> None:
    factory = _CleanupFactory(fail_open=True, close_mode="raise")
    deployment = open_extensions(_single(factory))
    with pytest.raises(ExtensionOpenFailed):
        await deployment.open()
    report = deployment.close_report
    assert not report.ok
    assert report.failed == ("example.reference: RuntimeError",)
    assert factory.marker not in str(report)
    assert factory.resources == 1, "the report must not pretend an uncooperative close released it"
    final = await deployment.aclose()
    assert not final.ok


async def test_a_failed_startup_without_close_is_unconfirmed() -> None:
    factory = _NoCloseFactory(fail_open=True)
    deployment = open_extensions(_single(factory))
    with pytest.raises(ExtensionOpenFailed):
        await deployment.open()
    report = deployment.close_report
    assert not report.ok
    assert report.unconfirmed == ("example.reference",)
    assert "close" not in " ".join(report.failed)


async def test_a_failed_startup_close_success_is_released() -> None:
    factory = _CleanupFactory(fail_open=True)
    deployment = open_extensions(_single(factory))
    with pytest.raises(ExtensionOpenFailed):
        await deployment.open()
    report = deployment.close_report
    assert report.ok
    assert report.released == ("example.reference",)


async def test_a_failed_startup_close_timeout_is_unconfirmed() -> None:
    factory = _CleanupFactory(fail_open=True, close_mode="timeout")
    deployment = open_extensions(_single(factory), close_timeout_seconds=0.01)
    with pytest.raises(ExtensionOpenFailed):
        await deployment.open()
    report = deployment.close_report
    assert not report.ok
    assert report.unconfirmed == ("example.reference",)


async def test_prior_factories_are_closed_when_a_later_factory_fails_cleanup() -> None:
    first = RecordingFactory(ExtensionContribution(tools=(read_tool("example.first.read"),)))
    second = _CleanupFactory(fail_open=True, close_mode="raise")
    plan = plan_extensions(
        manifests=[
            manifest(extension_id="example.first", tools=("example.first.read",), skills=()),
            manifest(extension_id="example.second", tools=("example.reference.write",), skills=()),
        ],
        factories={"example.first": first, "example.second": second},
        bindings=[
            binding(extension_id="example.first", tools=("example.first.read",), skills=()),
            binding(extension_id="example.second", tools=("example.reference.write",), skills=()),
        ],
    )
    deployment = open_extensions(plan)
    with pytest.raises(ExtensionOpenFailed):
        await deployment.open()
    report = deployment.close_report
    assert report.released == ("example.first",)
    assert report.failed == ("example.second: RuntimeError",)
    assert first.resources == 0 and second.resources == 1


async def test_a_factory_harness_error_canary_is_not_copied() -> None:
    canary = "factory-harness-canary-91ab"

    class Leaky:
        async def open(self, context):
            raise HarnessError(canary)

        async def close(self):
            return None

    deployment = open_extensions(_single(Leaky()))
    with pytest.raises(ExtensionOpenFailed) as caught:
        await deployment.open()
    assert canary not in str(caught.value)
    assert "HarnessError" in str(caught.value)
    assert deployment.close_report.released == ("example.reference",), (
        "a successful close of the failing factory must still be reported"
    )


async def test_close_report_does_not_copy_an_unbounded_exception_name() -> None:
    long_type = type("Marker" + "x" * 500, (RuntimeError,), {})
    factory = _CleanupFactory(close_mode="raise")
    factory.marker = "not copied"

    async def close():
        raise long_type("also not copied")

    factory.close = close  # type: ignore[method-assign]
    deployment = await open_extensions(_single(factory), close_timeout_seconds=1).open()
    report = await deployment.aclose()
    rendered = str(report)
    assert not report.ok
    assert "Markerxxxxxxxx" not in rendered
    assert len(rendered) < 500


def test_a_host_config_value_is_an_input_not_an_override(tmp_path) -> None:
    from ext_e2_deployment import RUN_ID, ScriptedStream, build, final_message, tool_call_message

    class Model:
        provider = "test"
        api = "test"

        def __init__(self, identifier: str) -> None:
            self.id = identifier

    async def scenario() -> None:
        first = await build(
            tmp_path / "state",
            model=Model("model-a"),
            fingerprints={"config": "fixed-host-config"},
            stream=ScriptedStream(tool_call_message()),
        )
        try:
            paused = await first.harness.start(RunRequest(prompt="write it"), first.context)
            assert paused.status.value == "suspended"
            await first.approvals.decide(
                paused.pending_interaction["approval_binding_key"],
                ApprovalStatus.APPROVED,
                "operator",
            )
            first_config = first.harness.fingerprints()["config"]
            assert first_config != "fixed-host-config"
        finally:
            await first.deployment.aclose()
            first.close()

        stream = ScriptedStream(final_message())
        second = await build(
            tmp_path / "state",
            attempt="a2",
            model=Model("model-b"),
            fingerprints={"config": "fixed-host-config"},
            stream=stream,
        )
        try:
            with pytest.raises(ResumeRefused) as refused:
                await second.harness.resume(RUN_ID, {}, second.context)
            assert refused.value.reason_code == "arrangement_drift"
            assert "config" in refused.value.drifted
            assert stream.calls == 0
            assert second.effect_lines() == []
            assert second.harness.fingerprints()["config"] != first_config
        finally:
            await second.deployment.aclose()
            second.close()

        same = await build(
            tmp_path / "state",
            attempt="a2",
            model=Model("model-a"),
            fingerprints={"config": "fixed-host-config"},
            stream=ScriptedStream(final_message()),
        )
        try:
            outcome = await same.harness.resume(RUN_ID, {}, same.context)
            assert outcome.status.value == "completed"
            assert same.effect_lines() == ["a2:reference.write:alpha"]
        finally:
            await same.deployment.aclose()
            same.close()

    asyncio.run(scenario())


async def test_a_factory_extension_error_canary_is_not_copied_either() -> None:
    canary = "factory-extension-canary-51df"

    class Leaky:
        async def open(self, context):
            raise ExtensionRefused(canary)

        async def close(self):
            return None

    deployment = open_extensions(_single(Leaky()))
    with pytest.raises(ExtensionOpenFailed) as caught:
        await deployment.open()
    assert canary not in str(caught.value)
    assert "ExtensionRefused" in str(caught.value)
