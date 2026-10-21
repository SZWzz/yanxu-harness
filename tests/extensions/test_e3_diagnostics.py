"""EXT-AC35: diagnostics are read-only and do not publish secrets or canaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yanxu_harness.extensions import (
    ExtensionContribution,
    ExtensionLimits,
    ExtensionOpenFailed,
    describe_plan,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.runtime import Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.storage import InMemoryPersistence
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolGateway,
)

from ext_support import RecordingFactory, binding, manifest, read_tool


def _context() -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"
        ),
        principal=Principal(subject="diagnostics"),
        labels={"authorization_scope": "authorized"},
    )


def test_a_nested_secret_is_absent_from_reports_messages_events_and_records(tmp_path: Path) -> None:
    secret = "nested-secret-2b6d"
    factory = RecordingFactory(ExtensionContribution(tools=(read_tool(),)))
    plan = plan_extensions(
        manifests=[manifest(tools=("example.reference.lookup",), skills=())],
        factories={"example.reference": factory},
        bindings=[
            binding(
                tools=("example.reference.lookup",),
                skills=(),
                resource_refs={"credential": secret, "workspace": str(tmp_path)},
            )
        ],
    )
    described = json.dumps(describe_plan(plan), ensure_ascii=False)
    assert secret not in described
    assert factory.opened == 0

    async def scenario() -> None:
        persistence = InMemoryPersistence()
        async with open_extensions(plan) as deployment:
            report = json.dumps(deployment.report(), ensure_ascii=False)
            assert secret not in report
            gateway = ToolGateway(
                registry=deployment.registry,
                policy=ConservativePolicy(),
                approvals=InMemoryApprovalStore(),
                backend=RecordingExecutionBackend(),
            )
            bindings = GatewayBindings(gateway=gateway, context=_context)
            harness = deployment.bind_harness(
                stream_fn=_Stream(),
                gateway_bindings=bindings,
                persistence=persistence,
            )
            outcome = await harness.start(RunRequest(prompt="look it up"), _context())
            assert outcome.status.value == "completed"

        record = await persistence.load_run("r1")
        assert record is not None
        assert secret not in json.dumps(record.state, ensure_ascii=False, default=str)
        events = await persistence.read_events()
        assert secret not in json.dumps(
            [event.payload for event in events], ensure_ascii=False, default=str
        )

    import asyncio

    asyncio.run(scenario())


def test_a_factory_exception_message_is_not_copied_into_the_public_error() -> None:
    canary = "exception-canary-c4e1"

    class Leaky:
        async def open(self, context):
            raise RuntimeError(f"the host secret is {canary}")

        async def close(self):
            return None

    factory = Leaky()

    async def open_it():
        plan = plan_extensions(
            manifests=[manifest()],
            factories={"example.reference": factory},
            bindings=[binding()],
        )
        async with open_extensions(plan):
            pass

    import asyncio

    with pytest.raises(ExtensionOpenFailed) as refused:
        asyncio.run(open_it())
    assert canary not in str(refused.value)
    assert "RuntimeError" in str(refused.value)


def test_the_report_limit_is_a_publication_bound_not_a_truncation() -> None:
    factory = RecordingFactory(ExtensionContribution())
    plan = plan_extensions(
        manifests=[manifest(tools=(), skills=())],
        factories={"example.reference": factory},
        bindings=[binding(tools=(), skills=())],
        limits=ExtensionLimits(max_report_bytes=64),
    )

    async def open_it() -> None:
        async with open_extensions(plan):
            pass

    import asyncio

    from yanxu_harness.extensions import ExtensionLimitExceeded

    with pytest.raises(ExtensionLimitExceeded, match="report"):
        asyncio.run(open_it())
    assert factory.closed == 1


class _Stream:
    """A tiny scripted stream: the model calls the lookup tool, then finishes."""

    def __init__(self) -> None:
        from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall

        self.messages = [
            AssistantMessage(
                content=[
                    ToolCall(
                        id="call-1",
                        name="example.reference.lookup",
                        arguments={"term": "alpha"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP),
        ]
        self.calls = 0

    async def __call__(self, model, context, options=None):
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self._final
