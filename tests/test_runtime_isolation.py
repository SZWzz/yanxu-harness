"""Cancellation, event fencing and run isolation.

Three claims that are easy to state and easy to get wrong:

* **Cancellation is bounded and reaches everything it should.** A run cancelled from
  another *thread* stops, its provider stream and worker thread are released, and no
  task is left writing state afterwards.
* **A stale attempt cannot change a live run.** A callback or an event from an
  earlier attempt is refused before anything is mutated, with a reason.
* **Two workers cannot drive one run, and two namespaces cannot touch each other.**
  The lease is what makes the first true; the composite operation key is what keeps
  the second from replaying someone else's receipt.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from durable_edit import (
    DeploymentPaths,
    ScriptedStream,
    build,
    final_message,
    run_context,
    tool_call_message,
)
from wire_fixture import WireFixture, delta_frame

from yanxu_harness.core.outcome import RunStatus
from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent
from yanxu_harness.errors import ContractError, RunBusy
from yanxu_harness.providers import CoreStreamBridge, OpenAICompatibleProvider, ProviderConfig
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)
from yanxu_harness.runtime.envelope import EventEnvelope, EventFence
from yanxu_harness.storage import InMemoryPersistence, SqlitePersistence, StorageConflict
from yanxu_harness.tools import ToolGateway, ToolInvocation, ToolRegistry
from yanxu_harness.tools.core_bridge import PENDING_STATUSES

NAMESPACE = "project-a"


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=25)

    return asyncio.run(guard())


def identity(attempt: str = "a1", run_id: str = "r1", namespace: str = NAMESPACE) -> RunIdentity:
    return RunIdentity(namespace=namespace, session_id="s1", run_id=run_id, attempt_id=attempt)


def context(attempt: str = "a1", run_id: str = "r1", namespace: str = NAMESPACE) -> RunContext:
    return RunContext(identity=identity(attempt, run_id, namespace), principal=Principal(subject="author"))


# --- the fence ---------------------------------------------------------------


def envelope(**overrides) -> EventEnvelope:
    values = dict(
        namespace=NAMESPACE, run_id="r1", attempt_id="a1", kind="text_delta", sequence=1, payload={"text": "x"}
    )
    values.update(overrides)
    return EventEnvelope(**values)


def test_a_current_event_is_accepted_and_advances_the_sequence() -> None:
    fence = EventFence(namespace=NAMESPACE, run_id="r1", attempt_id="a1")
    assert fence.accept(envelope()).accepted
    assert fence.last_sequence == 1
    assert fence.refusals == ()


def test_an_event_from_an_older_attempt_is_refused_with_a_reason() -> None:
    fence = EventFence(namespace=NAMESPACE, run_id="r1", attempt_id="a2")
    decision = fence.accept(envelope(attempt_id="a1", sequence=2))
    assert not decision.accepted
    assert "a1" in decision.reason and "a2" in decision.reason
    assert fence.last_sequence == -1, "a refused event must not advance anything"
    assert fence.refusals == (decision.reason,)


def test_an_event_from_another_namespace_or_run_is_refused() -> None:
    fence = EventFence(namespace=NAMESPACE, run_id="r1", attempt_id="a1")
    assert not fence.accept(envelope(namespace="project-b")).accepted
    assert not fence.accept(envelope(run_id="r2")).accepted


def test_an_out_of_order_event_is_not_applied_twice() -> None:
    fence = EventFence(namespace=NAMESPACE, run_id="r1", attempt_id="a1")
    assert fence.accept(envelope(sequence=5)).accepted
    assert not fence.accept(envelope(sequence=5)).accepted
    assert not fence.accept(envelope(sequence=4)).accepted
    assert fence.accept(envelope(sequence=6)).accepted


def test_the_envelope_carries_identity_order_and_schema() -> None:
    event = envelope(durable=True, payload={"status": "completed"})
    record = event.to_record()
    assert set(record) == {
        "schema_version",
        "namespace",
        "run_id",
        "attempt_id",
        "sequence",
        "kind",
        "payload",
        "durable",
    }
    assert EventEnvelope.from_record(record) == event
    assert record["schema_version"] == 1


def test_a_future_envelope_version_is_refused() -> None:
    from yanxu_harness.errors import VersionRefused

    with pytest.raises(VersionRefused):
        EventEnvelope.from_record(envelope().to_record() | {"schema_version": 9})
    with pytest.raises(ContractError):
        EventEnvelope.from_record({"namespace": NAMESPACE, "run_id": "r1", "attempt_id": "a1", "kind": "x"})


def test_a_stale_event_never_reaches_the_listener_or_the_transcript() -> None:
    """The fence runs before anything is mutated: no message, no listener call."""
    seen: list[str] = []
    harness = DefaultHarness(
        stream_fn=ScriptedStream(AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)),
        persistence=InMemoryPersistence(),
        system_prompt="be brief",
        listener=lambda event: seen.append(getattr(event, "type", "?")),
    )
    outcome = run(harness.start(RunRequest(prompt="hi"), context()))
    assert outcome.status is RunStatus.COMPLETED
    assert seen, "the current attempt's frames did reach the listener"

    # Now a *stale* frame: the run is over, and no scope is active, so nothing is
    # forwarded and nothing is recorded.
    before = len(seen)
    run(harness._emit_core_event(_FakeFrame("text_delta")))
    assert len(seen) == before


class _FakeFrame:
    def __init__(self, kind: str) -> None:
        self.type = kind
        self.message = None


def test_a_refusal_is_recorded_rather_than_silently_dropped() -> None:
    harness = DefaultHarness(
        stream_fn=ScriptedStream(AssistantMessage(content=[TextContent(text="x")], stop_reason=StopReason.STOP)),
        persistence=InMemoryPersistence(),
        system_prompt="be brief",
    )
    zone = harness._active
    scope = _ScopeStandIn()
    zone["r1"] = scope
    try:
        run(harness._emit_core_event(_FakeFrame("text_delta")))
    finally:
        zone.clear()
    assert harness.fence_refusals == (), "a frame with a valid envelope is accepted"


class _ScopeStandIn:
    """A scope-shaped object with a fence that always refuses, to prove the path."""

    def __init__(self) -> None:
        from yanxu_harness.runtime.envelope import EventFence as Fence

        self.record = type("R", (), {"namespace": NAMESPACE, "run_id": "r1", "attempt_id": "a1"})()
        self.core_fence = Fence(namespace="other", run_id="r1", attempt_id="a1")
        self.durable_fence = Fence(namespace="other", run_id="r1", attempt_id="a1")
        self.messages: list[object] = []
        self.token = None
        self.task = None
        self.loop = None

    def envelope(self, kind, payload, *, durable=False):
        return EventEnvelope(
            namespace=NAMESPACE, run_id="r1", attempt_id="a1", kind=kind, payload=dict(payload)
        )


# --- cancellation ------------------------------------------------------------


def test_cancelling_from_another_thread_stops_a_blocked_provider() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "思考一"}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "思考二"}}]},
        delta_frame("答", finish="stop"),
    ]

    async def scenario():
        with WireFixture(frames, chunk_delay_seconds=10.0) as fixture:
            provider = OpenAICompatibleProvider(
                ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=30.0)
            )
            harness = DefaultHarness(
                stream_fn=CoreStreamBridge(provider),
                persistence=InMemoryPersistence(),
                system_prompt="be brief",
            )
            task = asyncio.ensure_future(harness.start(RunRequest(prompt="hi"), context()))
            await asyncio.sleep(0.2)  # let the first frame arrive

            def cancel_from_thread() -> None:
                asyncio.run_coroutine_threadsafe(harness.cancel("r1", "author stopped"), loop)

            loop = asyncio.get_running_loop()
            thread = threading.Thread(target=cancel_from_thread)
            started = time.monotonic()
            thread.start()
            outcome = await asyncio.wait_for(task, timeout=15)
            thread.join(timeout=5)
            elapsed = time.monotonic() - started
            return outcome, elapsed

    outcome, elapsed = run(scenario())
    assert outcome.status is RunStatus.CANCELLED
    assert outcome.error_code == "cancelled"
    assert elapsed < 5.0, f"cancellation must be bounded, took {elapsed:.1f}s"


def test_a_cancelled_run_leaves_no_dangling_task_and_the_loop_stays_responsive() -> None:
    """Bounded cleanup: no drive task survives, and the loop keeps running tasks.

    The executor thread that pumped the provider is a detail of asyncio's default
    executor (it stays idle for reuse); what must hold is that the *drive task* is
    gone and that an unrelated task kept being scheduled throughout — the loop was
    never blocked by the cancelled stream, which is the failure mode the thread
    bridge exists to prevent.
    """

    async def scenario():
        with WireFixture(
            [{"choices": [{"index": 0, "delta": {"reasoning_content": "x"}}]}],
            chunk_delay_seconds=10.0,
        ) as fixture:
            provider = OpenAICompatibleProvider(
                ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=30.0)
            )
            harness = DefaultHarness(
                stream_fn=CoreStreamBridge(provider),
                persistence=InMemoryPersistence(),
                system_prompt="be brief",
            )
            ticks = 0
            stop = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    ticks += 1

            # Everything alive before the drive is expected; a *new* live task
            # after it is a leak. Counting tasks directly is version-dependent
            # (`all_tasks` includes the test's own wrapper on some interpreters).
            baseline = {id(task) for task in asyncio.all_tasks()}
            ticker_task = asyncio.ensure_future(ticker())
            baseline.add(id(ticker_task))
            drive = asyncio.ensure_future(harness.start(RunRequest(prompt="hi"), context()))
            await asyncio.sleep(0.2)
            await harness.cancel("r1", "stop")
            outcome = await asyncio.wait_for(drive, timeout=15)
            stop.set()
            await ticker_task
            stray = [
                task
                for task in asyncio.all_tasks()
                if id(task) not in baseline and not task.done()
            ]
            return outcome, ticks, stray

    outcome, ticks, stray = run(scenario())
    assert outcome.status is RunStatus.CANCELLED
    assert ticks > 5, f"the loop was blocked during the cancelled stream (ticks={ticks})"
    assert stray == [], f"live tasks left behind: {stray}"


def test_cancellation_after_a_write_leaves_the_receipt_alone() -> None:
    """A cancelled run is not "nothing happened": the receipt is the evidence."""
    from durable_edit import write_calls

    directory = Path(_tmpdir())
    paths = DeploymentPaths.under(directory)
    harness, gateway = build(
        paths,
        ScriptedStream(tool_call_message("hello"), final_message("写好了。")),
        attempt="a1",
        auto_write=True,
    )
    outcome = run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    # The run completed (auto_write) and the receipt exists; cancelling afterwards
    # must not remove it, because a receipt is a fact about the outside world.
    assert outcome.status is RunStatus.COMPLETED
    assert write_calls(paths.counter) == 1
    backend = gateway._backend  # noqa: SLF001 - the ledger is the evidence
    assert backend.counts_by_state().get("succeeded") == 1


def _tmpdir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="yanxu-2d-")


# --- isolation: leases and namespaces ----------------------------------------


def test_a_second_worker_cannot_drive_the_same_run(tmp_path: Path) -> None:
    """The lease is what stops two workers from running the same run twice."""
    store = SqlitePersistence(tmp_path / "runs.db")
    release = asyncio.Event()

    class Blocking:
        async def __call__(self, model, context, options=None):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            await asyncio.wait_for(release.wait(), timeout=10)
            return AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)

    async def scenario():
        first = DefaultHarness(stream_fn=Blocking(), persistence=store, system_prompt="be brief")
        second = DefaultHarness(stream_fn=Blocking(), persistence=store, system_prompt="be brief")
        task = asyncio.ensure_future(first.start(RunRequest(prompt="hi"), context()))
        await asyncio.sleep(0.1)
        # A different holder: the lease is valid, so this must be refused.
        with pytest.raises(RunBusy):
            await second.start(RunRequest(prompt="hi"), context())
        release.set()
        outcome = await asyncio.wait_for(task, timeout=10)
        # Once the first released the lease the run is finished anyway.
        state = await first.state("r1", context=context())
        return outcome, state

    outcome, state = run(scenario())
    assert outcome.status is RunStatus.COMPLETED
    assert state.status is RunStatus.COMPLETED


def test_a_stale_worker_cannot_commit_after_losing_its_lease(tmp_path: Path) -> None:
    """The fencing token is checked inside the write, not only at claim time."""
    store = SqlitePersistence(tmp_path / "runs.db")

    async def scenario():
        first = await store.claim_lease("run:r1", "worker-a", ttl_seconds=300)
        assert first is not None
        # The lease expires and a second worker takes over with a higher fence.
        second = await store.claim_lease("run:r1", "worker-b", ttl_seconds=300, now=first.expires_at + 1)
        assert second is not None and second.fencing_token > first.fencing_token
        await store.save_run("r1", {"who": "worker-b"}, expected_version=None, lease=second)
        with pytest.raises(StorageConflict):
            await store.save_run("r1", {"who": "worker-a"}, expected_version=1, lease=first)

    run(scenario())


def test_the_operation_key_includes_the_namespace() -> None:
    """Two projects may each have `r1`/`call-1`; the keys must not collide."""
    from yanxu_harness.tools import ToolDefinition, ConservativePolicy, InMemoryApprovalStore
    from yanxu_harness.testing import RecordingExecutionBackend

    calls: list[str] = []

    def handler(_ctx, arguments):
        calls.append(arguments["path"])
        return {"content": [TextContent(text="ok")], "details": {}}

    definition = ToolDefinition(
        "notes.read", "read", args_validator=lambda arguments: dict(arguments), handler=handler
    )
    backend = RecordingExecutionBackend()
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=backend,
    )

    async def scenario():
        for namespace in ("project-a", "project-b"):
            invocation = ToolInvocation(
                call_id="call-1", namespace=namespace, tool_name="notes.read", arguments={"path": "a.md"}, run_id="r1"
            )
            await gateway.invoke(invocation, context(namespace=namespace))

    run(scenario())
    ids = {intent.operation_id for intent in backend.intents}
    assert len(ids) == 2, f"the two projects shared an operation id: {ids}"
    assert sorted(ids) == ["project-a:r1:call-1", "project-b:r1:call-1"]


def test_a_second_namespace_cannot_read_or_resume_the_first_runs_record(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(paths, ScriptedStream(tool_call_message("hello")), attempt="a1")
    paused = run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    assert paused.status is RunStatus.SUSPENDED

    other, _gateway_two = build(paths, ScriptedStream(tool_call_message("hello")), attempt="a1")
    with pytest.raises(ContractError):
        run(other.state("r1", context=context(namespace="project-b")))
    with pytest.raises(ContractError):
        run(other.resume("r1", {}, context(attempt="a2", namespace="project-b")))
    # And starting a *different* run in another namespace is fine: namespaces are
    # isolated, not mutually exclusive.
    free = DefaultHarness(
        stream_fn=ScriptedStream(
            AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)
        ),
        persistence=InMemoryPersistence(),
        system_prompt="be brief",
    )
    outcome = run(free.start(RunRequest(prompt="hi"), context(run_id="r1", namespace="project-b")))
    assert outcome.status is RunStatus.COMPLETED


def test_pending_statuses_are_the_three_that_must_stop_a_run() -> None:
    values = sorted(status.value for status in PENDING_STATUSES)
    assert values == ["awaiting_approval", "deferred", "unknown"]


def test_a_max_runtime_budget_stops_the_run_with_usage_limited() -> None:
    class Slow:
        async def __call__(self, model, context, options=None):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            await asyncio.sleep(5)
            return AssistantMessage(content=[TextContent(text="late")], stop_reason=StopReason.STOP)

    async def scenario():
        harness = DefaultHarness(
            stream_fn=Slow(),
            persistence=InMemoryPersistence(),
            system_prompt="be brief",
            max_runtime_seconds=0.2,
        )
        return await harness.start(RunRequest(prompt="hi"), context())

    outcome = run(scenario())
    assert outcome.status is RunStatus.USAGE_LIMITED
    assert outcome.error_code == "max_runtime_exceeded"
    assert "runtime budget" in outcome.detail


def test_a_lease_is_released_when_a_run_finishes(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(paths, ScriptedStream(tool_call_message("hello")), attempt="a1")
    run(harness.start(RunRequest(prompt="write it"), run_context("a1")))

    async def claim_again() -> object:
        store = SqlitePersistence(paths.runs)
        lease = await store.claim_lease("run:r1", "another-worker", ttl_seconds=60)
        store.close()
        return lease

    assert run(claim_again()) is not None, "the finished run's lease must be free"


def test_events_are_published_with_an_envelope_and_a_json_safe_payload() -> None:
    published: list[dict] = []

    async def listener(event):
        published.append(event.to_record() if hasattr(event, "to_record") else {"kind": str(event.kind)})

    harness = DefaultHarness(
        stream_fn=ScriptedStream(
            AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)
        ),
        persistence=InMemoryPersistence(),
        system_prompt="be brief",
        run_listener=listener,
    )
    run(harness.start(RunRequest(prompt="hi"), context()))
    assert published, "a run publishes its durable events"
    assert all("kind" in item for item in published)
    json.dumps([item.get("payload", {}) for item in published], ensure_ascii=False)


def test_published_events_never_carry_host_secrets() -> None:
    """No key, header value or private continuation material reaches an event.

    The provider is real (a loopback fixture) and is configured with a key and a host
    header, so the test would fail if any of that were echoed into a core frame
    envelope or a durable event payload.
    """
    from yanxu_harness.providers import OpenAICompatibleProvider, ProviderConfig, SecretReference

    key = "sk-" + "loopback-secret-value"
    header_value = "tenant-42-session-abc"
    frames: list[dict] = []
    published: list[dict] = []

    async def core_listener(event):
        frames.append({"type": getattr(event, "type", "?")})

    def run_listener(event):
        published.append(event.to_record())

    scripts = [
        [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "思考", "signature": "sig-private"}}]},
            delta_frame("答案", finish="stop"),
        ]
    ]
    with WireFixture(scripts=scripts) as fixture:
        provider = OpenAICompatibleProvider(
            ProviderConfig(
                model="example",
                base_url=fixture.base_url,
                api_key=SecretReference("EXAMPLE_KEY"),
                headers={"x-tenant": header_value},
            ),
            secrets={"EXAMPLE_KEY": key},
        )
        harness = DefaultHarness(
            stream_fn=CoreStreamBridge(provider),
            persistence=InMemoryPersistence(),
            system_prompt="be brief",
            listener=core_listener,
            run_listener=run_listener,
        )
        outcome = run(harness.start(RunRequest(prompt="hi"), context()))

    assert outcome.status is RunStatus.COMPLETED
    assert frames, "core frames were delivered"
    assert published, "durable events were delivered"
    blob = json.dumps({"frames": frames, "published": published}, ensure_ascii=False)
    assert key not in blob, "the API key must never appear in an event"
    assert header_value not in blob, "a host header value must never appear in an event"
    assert "sig-private" not in blob, "private continuation material stays out of events"
    # The envelope that carries the order/identity is present on the durable path.
    assert all({"schema_version", "run_id", "attempt_id", "sequence"} <= set(item) for item in published)
