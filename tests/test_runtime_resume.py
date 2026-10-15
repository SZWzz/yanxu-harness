"""Suspending on an approval, restarting, and resuming: the chain end to end.

The claim under test is the one the whole design exists for: a run pauses before a
write, the process goes away, a *new* process with a *new* harness instance reads
the pause from disk, the operator's decision is honoured once, and the model sees
the real result of the call it made — while nothing that may already have happened
is run a second time.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from yanxu_harness.core.outcome import LoopStop, RunStatus
from yanxu_harness.core.types import (
    AssistantMessage,
    StopReason,
    TextContent,
    ToolCall,
)
from yanxu_harness.errors import ContractError
from yanxu_harness.events import EventKind
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)
from yanxu_harness.runtime.batch import CallOutcome, InteractionKind, RunBatch
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import Durability, InMemoryPersistence
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

NAMESPACE = "project-a"


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=20)

    return asyncio.run(guard())


def identity(attempt: str = "a1", run_id: str = "r1", namespace: str = NAMESPACE) -> RunIdentity:
    return RunIdentity(namespace=namespace, session_id="s1", run_id=run_id, attempt_id=attempt)


def context(attempt: str = "a1", run_id: str = "r1", namespace: str = NAMESPACE):
    return RunContext(identity=identity(attempt, run_id, namespace), principal=Principal(subject="author"))


def write_definition(handler) -> ToolDefinition:
    return ToolDefinition(
        "notes.write",
        "append to a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=lambda arguments: {"path": arguments["path"], "text": arguments["text"]},
        handler=handler,
        preview=lambda _ctx, arguments: {"path": arguments["path"], "text": arguments["text"]},
    )


class Calls:
    """A handler that records every call, so "ran once" is provable."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def __call__(self, ctx, arguments):
        self.seen.append(
            {
                "attempt": ctx.identity.attempt_id,
                "arguments": dict(arguments),
                "tool_call_id": ctx.tool_call_id,
            }
        )
        return {"content": [TextContent(text=f"wrote {arguments['text']}")], "details": {"ok": True}}


class Script:
    """A stream_fn that replays one assistant message per request."""

    def __init__(self, *messages: AssistantMessage) -> None:
        self.messages = list(messages)
        self.calls = 0

    async def __call__(self, model, context, options=None):
        await asyncio.sleep(0)
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self._final


def tool_call_message(call_id: str = "call-1", text: str = "hello") -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(id=call_id, name="notes.write", arguments={"path": "a.md", "text": text})],
        stop_reason=StopReason.TOOL_USE,
    )


def final_message(text: str = "写好了。") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


class Deployment:
    """One process's view: a store, an approval ledger, a gateway and a harness."""

    def __init__(
        self,
        store,
        approvals,
        backend,
        handler,
        *,
        fingerprints=None,
        run_context=None,
        **harness_kwargs,
    ):
        self.definition = write_definition(handler)
        self.run_context = run_context or context()
        self.gateway = ToolGateway(
            registry=ToolRegistry([self.definition]),
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=backend,
        )
        # A callable, so the identity is resolved at call time: a resumed attempt must
        # be what the handler sees, not the attempt the harness was built with.
        self.bindings = GatewayBindings(gateway=self.gateway, context=lambda: self.run_context)
        self.tool = core_tool(self.definition, self.bindings)
        self.harness = DefaultHarness(
            stream_fn=harness_kwargs.pop("stream_fn"),
            tools=[self.tool],
            persistence=store,
            system_prompt="be brief",
            fingerprints=fingerprints,
            **harness_kwargs,
        )


def first_turn_only(handler):
    """The first deployment: the model asks for the write, then the run pauses."""
    return Deployment(
        InMemoryPersistence(),
        InMemoryApprovalStore(),
        RecordingExecutionBackend(),
        handler,
        stream_fn=Script(tool_call_message()),
    )


# --- the pause itself --------------------------------------------------------


def test_a_write_pauses_before_the_next_model_request_and_is_persisted() -> None:
    handler = Calls()
    deployment = first_turn_only(handler)
    requests: list[object] = []

    original = deployment.harness._stream_fn  # noqa: SLF001 - asserting on the request count

    class Counting(Script):
        async def __call__(self, model, context, options=None):
            requests.append(context)
            return await original(model, context, options)

    deployment.harness._stream_fn = Counting(tool_call_message())  # noqa: SLF001

    outcome = run(deployment.harness.start(RunRequest(prompt="write it"), context()))

    assert outcome.status is RunStatus.SUSPENDED
    assert outcome.stop_code == LoopStop.SUSPENDED.value
    assert len(requests) == 1, "a waiting run must not ask the model again"
    assert handler.seen == [], "nothing may run before the decision"

    # The pause is durable: a new instance reads the batch, the interaction and the
    # transcript out of the store.
    record, _ = run(deployment.harness._load("r1"))  # noqa: SLF001
    assert record is not None and record.status is RunStatus.SUSPENDED
    assert record.batch is not None
    batch = RunBatch.from_record(record.batch)
    assert [call.call_id for call in batch.calls] == ["call-1"]
    assert [call.outcome for call in batch.calls] == [CallOutcome.AWAITING]
    assert batch.interactions[0].kind is InteractionKind.APPROVAL
    assert batch.interactions[0].approval_binding_key
    assert batch.tools_fingerprint and batch.exposure_manifest == {"tools": ["notes.write"]}
    assert outcome.pending_interaction["kind"] == "approval"
    assert outcome.pending_interaction["preview"] == {"path": "a.md", "text": "hello"}


def test_the_pause_is_committed_with_its_events() -> None:
    deployment = first_turn_only(Calls())
    run(deployment.harness.start(RunRequest(prompt="write it"), context()))

    events = run(deployment.harness._persistence.read_events())  # noqa: SLF001
    kinds = [event.kind for event in events]
    assert EventKind.RUN_STARTED.value in kinds
    assert EventKind.APPROVAL_REQUESTED.value in kinds
    assert EventKind.RUN_SUSPENDED.value in kinds


def test_a_listener_failure_does_not_roll_back_or_lose_the_events() -> None:
    """A failed fanout is repaired by replay; the committed fact stays committed."""
    deployment = first_turn_only(Calls())
    seen: list[str] = []

    def failing(event):
        # The run listener receives the public envelope: kind is the event name.
        seen.append(event.kind)
        raise RuntimeError("the consumer is down")

    deployment.harness._run_listener = failing  # noqa: SLF001
    outcome = run(deployment.harness.start(RunRequest(prompt="write it"), context()))

    assert outcome.status is RunStatus.SUSPENDED
    assert seen, "the listener was called"
    assert "run_suspended" in deployment.harness.fanout_failures
    record, _ = run(deployment.harness._load("r1"))  # noqa: SLF001
    assert record is not None and record.status is RunStatus.SUSPENDED, "no rollback"

    # Replay from the cursor delivers what the broken consumer missed.
    published: list[str] = []

    async def collect(event):
        published.append(event.kind)

    deployment.harness._run_listener = collect  # noqa: SLF001
    replayed = run(deployment.harness.replay_run_events("r1"))
    assert [event.kind for event in replayed] == published
    assert EventKind.RUN_SUSPENDED.value in published


# --- resume, with and without authorisation ---------------------------------


def test_resuming_without_a_decision_asks_again_and_runs_nothing() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    run(first.harness.start(RunRequest(prompt="write it"), context()))

    # A new process: new harness instance, same store and ledger.
    second = Deployment(
        store,
        approvals,
        backend,
        handler,
        stream_fn=Script(final_message()),
        fingerprints=first.harness.fingerprints(),
        run_context=context(attempt="a2"),
    )
    outcome = run(second.harness.resume("r1", {}, context(attempt="a2")))

    assert outcome.status is RunStatus.SUSPENDED
    assert handler.seen == [] and backend.receipts == []
    state = run(second.harness.state("r1"))
    assert state.attempt_id == "a2", "the attempt advanced even though nothing ran"
    assert state.pending_interaction["kind"] == "approval"


def test_a_client_supplied_approved_flag_is_not_authorisation() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    run(first.harness.start(RunRequest(prompt="write it"), context()))
    second = Deployment(
        store,
        approvals,
        backend,
        handler,
        stream_fn=Script(final_message()),
        fingerprints=first.harness.fingerprints(),
        run_context=context(attempt="a2"),
    )

    outcome = run(
        second.harness.resume("r1", {"approved": True, "actor": "somebody"}, context(attempt="a2"))
    )

    assert outcome.status is RunStatus.SUSPENDED
    assert handler.seen == [], "an approval claim in the resolution must change nothing"
    assert backend.receipts == []


def test_the_host_decision_is_honoured_once_and_the_model_sees_the_result() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    paused = run(first.harness.start(RunRequest(prompt="write it"), context()))
    binding_key = paused.pending_interaction["approval_binding_key"]

    async def approve() -> None:
        await approvals.decide(binding_key, ApprovalStatus.APPROVED, decided_by="operator")

    run(approve())

    # A new process resumes: the same gateway, the same ledger, a new attempt.
    stream = Script(final_message("写好了。"))
    second = Deployment(
        store,
        approvals,
        backend,
        handler,
        stream_fn=stream,
        fingerprints=first.harness.fingerprints(),
        run_context=context(attempt="a2"),
    )
    outcome = run(second.harness.resume("r1", {}, context(attempt="a2")))

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.final_text == "写好了。"
    assert [call["attempt"] for call in handler.seen] == ["a2"], "the handler ran once, under the new attempt"
    assert len(backend.receipts) == 1
    assert stream.calls == 1, "the model is asked exactly once after the decision"

    # The resumed model saw the tool's real result, bound to the original call id.
    record, _ = run(second.harness._load("r1"))  # noqa: SLF001
    transcript = [json.loads(json.dumps(item)) for item in record.messages]
    tool_results = [item for item in transcript if item["role"] == "toolResult"]
    assert [item["tool_call_id"] for item in tool_results] == ["call-1"]
    assert "wrote hello" in tool_results[0]["content"][0]["text"]
    assert record.attempt_id == "a2"

    # Resuming again is refused: the run is finished, and its outcome is not rewritten.
    with pytest.raises(ContractError):
        run(second.harness.resume("r1", {}, context(attempt="a3")))


def test_a_denied_call_reaches_the_model_instead_of_running() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    paused = run(first.harness.start(RunRequest(prompt="write it"), context()))
    binding_key = paused.pending_interaction["approval_binding_key"]

    async def deny() -> None:
        await approvals.decide(binding_key, ApprovalStatus.DENIED, decided_by="operator", note="not now")

    run(deny())
    stream = Script(final_message("好，不写了。"))
    second = Deployment(
        store,
        approvals,
        backend,
        handler,
        stream_fn=stream,
        fingerprints=first.harness.fingerprints(),
        run_context=context(attempt="a2"),
    )
    outcome = run(second.harness.resume("r1", {}, context(attempt="a2")))

    assert outcome.status is RunStatus.COMPLETED
    assert handler.seen == [], "a denied write must not run"
    record, _ = run(second.harness._load("r1"))  # noqa: SLF001
    tool_result = [item for item in record.messages if item["role"] == "toolResult"][0]
    assert tool_result["is_error"] is True
    assert "denied" in tool_result["content"][0]["text"]
    assert stream.calls == 1


def test_an_expired_approval_goes_back_to_the_author_instead_of_running() -> None:
    """Expiry means "ask again": the gateway supersedes the binding on re-dispatch."""
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    paused = run(first.harness.start(RunRequest(prompt="write it"), context()))
    binding_key = paused.pending_interaction["approval_binding_key"]

    async def approve_then_expire() -> None:
        await approvals.decide(binding_key, ApprovalStatus.APPROVED, decided_by="operator")
        # Rewrite the binding with an expiry in the past: the approval is valid as a
        # decision but no longer applies to this moment.
        stored = await approvals.get(binding_key)
        assert stored is not None
        from dataclasses import replace

        expired = replace(stored, binding=replace(stored.binding, expires_at=1.0))
        approvals._approvals[binding_key] = expired  # noqa: SLF001 - the test owns the ledger

    run(approve_then_expire())
    second = Deployment(
        store,
        approvals,
        backend,
        handler,
        stream_fn=Script(final_message()),
        fingerprints=first.harness.fingerprints(),
        run_context=context(attempt="a2"),
    )
    outcome = run(second.harness.resume("r1", {}, context(attempt="a2")))

    assert outcome.status is RunStatus.SUSPENDED, "an expired approval does not authorise a write"
    assert handler.seen == []
    assert outcome.pending_interaction["kind"] == "approval"


# --- batches that are partly done -------------------------------------------


def test_a_settled_call_is_reused_and_only_the_waiting_one_is_dispatched() -> None:
    handler = Calls()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    # One read tool that runs immediately, one write that waits.
    read_handler = Calls()

    def read_handler_fn(_ctx, arguments):
        read_handler.seen.append({"arguments": dict(arguments)})
        return {"content": [TextContent(text=f"read {arguments['path']}")], "details": {"ok": True}}

    read = ToolDefinition(
        "notes.read",
        "read a note",
        args_validator=lambda arguments: {"path": arguments["path"]},
        handler=read_handler_fn,
    )
    write = write_definition(handler)
    store_two = InMemoryPersistence()
    deployment = Deployment.__new__(Deployment)
    deployment.definition = write
    deployment.gateway = ToolGateway(
        registry=ToolRegistry([read, write]),
        policy=ConservativePolicy(),
        approvals=approvals,
        backend=backend,
    )
    deployment.bindings = GatewayBindings(gateway=deployment.gateway, context=context)
    read_tool = core_tool(read, deployment.bindings)
    write_tool = core_tool(write, deployment.bindings)
    batch_message = AssistantMessage(
        content=[
            ToolCall(id="c-read", name="notes.read", arguments={"path": "a.md"}),
            ToolCall(id="c-write", name="notes.write", arguments={"path": "a.md", "text": "hi"}),
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    deployment.harness = DefaultHarness(
        stream_fn=Script(batch_message),
        tools=[read_tool, write_tool],
        persistence=store_two,
        system_prompt="be brief",
    )

    paused = run(deployment.harness.start(RunRequest(prompt="do both"), context()))
    assert paused.status is RunStatus.SUSPENDED
    assert len(read_handler.seen) == 1, "the read tool ran; only the write waits"
    assert handler.seen == []
    binding_key = paused.pending_interaction["approval_binding_key"]

    record, _ = run(deployment.harness._load("r1"))  # noqa: SLF001
    paused_batch = RunBatch.from_record(record.batch)
    outcomes = {call.call_id: call.outcome for call in paused_batch.calls}
    assert outcomes == {"c-read": CallOutcome.SETTLED, "c-write": CallOutcome.AWAITING}

    async def approve() -> None:
        await approvals.decide(binding_key, ApprovalStatus.APPROVED, decided_by="operator")

    run(approve())
    second = Deployment(
        store_two, approvals, backend, handler, stream_fn=Script(final_message("both done")),
        fingerprints=deployment.harness.fingerprints(),
    )
    # The second deployment must expose both tools, or the drift check refuses.
    second.gateway = deployment.gateway
    second.bindings = GatewayBindings(gateway=deployment.gateway, context=context)
    second.tool = core_tool(write, second.bindings)
    second.harness = DefaultHarness(
        stream_fn=Script(final_message("both done")),
        tools=[core_tool(read, second.bindings), second.tool],
        persistence=store_two,
        system_prompt="be brief",
        fingerprints=deployment.harness.fingerprints(),
    )

    outcome = run(second.harness.resume("r1", {}, context(attempt="a2")))

    assert outcome.status is RunStatus.COMPLETED
    assert len(read_handler.seen) == 1, "the settled read call was reused, not re-run"
    assert len(handler.seen) == 1, "the write ran exactly once"
    write_operation_id = next(
        call.operation_id for call in paused_batch.calls if call.call_id == "c-write"
    )
    write_receipts = [
        item for item in backend.receipts if item.operation_id == write_operation_id
    ]
    assert len(write_receipts) == 1, "the write has exactly one receipt"
    # Results reach the model in the original call order.
    record, _ = run(second.harness._load("r1"))  # noqa: SLF001
    tool_results = [item for item in record.messages if item["role"] == "toolResult"]
    assert [item["tool_call_id"] for item in tool_results] == ["c-read", "c-write"]
    assert "wrote hi" in tool_results[1]["content"][0]["text"]
    assert run(store_two.load_run("r1")).state["batch"] is None


# --- unknown outcomes --------------------------------------------------------


def test_an_unproven_operation_stays_unknown_and_asks_for_evidence() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    # The backend reports an unproven effect: no reconciler is configured here.
    backend = RecordingExecutionBackend(state=__import__("yanxu_harness.tools", fromlist=["EffectState"]).EffectState.UNKNOWN)
    first = Deployment(
        store, approvals, backend, handler, stream_fn=Script(tool_call_message()),
        fingerprints=None,
    )
    # `auto_write` so the call reaches execution instead of waiting for approval.
    first.gateway = ToolGateway(
        registry=ToolRegistry([first.definition]),
        policy=ConservativePolicy(auto_write=True),
        approvals=approvals,
        backend=backend,
    )
    first.bindings = GatewayBindings(gateway=first.gateway, context=context)
    first.harness = DefaultHarness(
        stream_fn=Script(tool_call_message()),
        tools=[core_tool(first.definition, first.bindings)],
        persistence=store,
        system_prompt="be brief",
    )

    paused = run(first.harness.start(RunRequest(prompt="write it"), context()))

    assert paused.status is RunStatus.SUSPENDED
    assert paused.pending_interaction["kind"] == "reconciliation"
    assert len(handler.seen) == 1, "the handler ran once; the outcome is what is unproven"

    second = Deployment(
        store,
        approvals,
        backend,
        handler,
        stream_fn=Script(final_message()),
        fingerprints=first.harness.fingerprints(),
        run_context=context(attempt="a2"),
    )
    second.gateway = first.gateway
    second.bindings = GatewayBindings(gateway=first.gateway, context=context)
    second.harness = DefaultHarness(
        stream_fn=Script(final_message()),
        tools=[core_tool(first.definition, second.bindings)],
        persistence=store,
        system_prompt="be brief",
    )

    still_unknown = run(second.harness.resume("r1", {}, context(attempt="a2")))
    assert still_unknown.status is RunStatus.SUSPENDED
    assert still_unknown.pending_interaction["kind"] == "reconciliation"
    assert len(handler.seen) == 1, "an unproven operation is never retried automatically"


def test_business_evidence_settles_an_unproven_operation() -> None:
    from yanxu_harness.tools import EffectState

    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend(state=EffectState.UNKNOWN)
    definition = write_definition(handler)
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(auto_write=True),
        approvals=approvals,
        backend=backend,
    )
    bindings = GatewayBindings(gateway=gateway, context=context)
    first = DefaultHarness(
        stream_fn=Script(tool_call_message()),
        tools=[core_tool(definition, bindings)],
        persistence=store,
        system_prompt="be brief",
    )
    paused = run(first.start(RunRequest(prompt="write it"), context()))
    assert paused.status is RunStatus.SUSPENDED

    asked: list[str] = []

    async def reconciler(call, details):
        asked.append(details["operation_id"])
        return {"proven": True, "state": "succeeded", "detail": "the note contains hello", "source": "business store"}

    second = DefaultHarness(
        stream_fn=Script(final_message("确认写入了。")),
        tools=[core_tool(definition, bindings)],
        persistence=store,
        system_prompt="be brief",
        reconcile=reconciler,
    )
    outcome = run(second.resume("r1", {}, context(attempt="a2")))

    assert outcome.status is RunStatus.COMPLETED
    assert len(asked) == 1 and asked[0].endswith(":call-1")
    assert len(handler.seen) == 1, "evidence settles the operation; nothing re-runs it"
    record, _ = run(second._load("r1"))  # noqa: SLF001
    tool_result = [item for item in record.messages if item["role"] == "toolResult"][0]
    assert "confirmed by business evidence" in tool_result["content"][0]["text"] or "the note contains hello" in tool_result["content"][0]["text"]
    batch = RunBatch.from_record(record.batch) if record.batch else None
    # The batch was cleared once everything settled; the evidence is in the message.
    assert batch is None


def test_a_reconciler_claim_without_evidence_is_refused() -> None:
    from yanxu_harness.tools import EffectState

    handler = Calls()
    definition = write_definition(handler)
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=RecordingExecutionBackend(state=EffectState.UNKNOWN),
    )
    bindings = GatewayBindings(gateway=gateway, context=context)
    store = InMemoryPersistence()
    first = DefaultHarness(
        stream_fn=Script(tool_call_message()),
        tools=[core_tool(definition, bindings)],
        persistence=store,
        system_prompt="be brief",
    )
    run(first.start(RunRequest(prompt="write it"), context()))

    async def bogus(call, details):
        return {"state": "succeeded"}  # no proven=True

    second = DefaultHarness(
        stream_fn=Script(final_message()),
        tools=[core_tool(definition, bindings)],
        persistence=store,
        system_prompt="be brief",
        reconcile=bogus,
    )
    with pytest.raises(ContractError):
        run(second.resume("r1", {}, context(attempt="a2")))


# --- drift -------------------------------------------------------------------


def test_a_changed_tool_set_refuses_the_resume_with_the_drift_named() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    run(first.harness.start(RunRequest(prompt="write it"), context()))

    # The new deployment exposes a different tool set: the approval was granted for
    # the old arrangement, so the resume must not proceed under it.
    second = Deployment(
        store, approvals, backend, handler, stream_fn=Script(final_message()),
        fingerprints=first.harness.fingerprints(),
    )
    second.harness._tools = (*second.harness._tools, core_tool(second.definition, second.bindings))  # noqa: SLF001

    with pytest.raises(ContractError) as excinfo:
        run(second.harness.resume("r1", {}, context(attempt="a2")))
    assert "drifted" in str(excinfo.value)
    assert "exposure" in str(excinfo.value) or "tools" in str(excinfo.value)
    assert handler.seen == []


# --- identity ----------------------------------------------------------------


def test_a_suspended_run_cannot_be_resumed_from_another_namespace() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    run(first.harness.start(RunRequest(prompt="write it"), context()))
    with pytest.raises(ContractError):
        run(first.harness.resume("r1", {}, context(attempt="a2", namespace="project-b")))


def test_a_resumed_run_keeps_namespace_session_and_run_and_advances_the_attempt() -> None:
    handler = Calls()
    store = InMemoryPersistence()
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    first = Deployment(store, approvals, backend, handler, stream_fn=Script(tool_call_message()))
    paused = run(first.harness.start(RunRequest(prompt="write it"), context()))
    run(_approve(approvals, paused.pending_interaction["approval_binding_key"]))

    second = Deployment(
        store,
        approvals,
        backend,
        handler,
        stream_fn=Script(final_message()),
        fingerprints=first.harness.fingerprints(),
        run_context=context(attempt="a2"),
    )
    outcome = run(second.harness.resume("r1", {}, context(attempt="a7")))
    record, _ = run(second.harness._load("r1"))  # noqa: SLF001
    assert (record.namespace, record.session_id, record.run_id) == (NAMESPACE, "s1", "r1")
    assert record.attempt_id == "a7"
    assert outcome.status is RunStatus.COMPLETED
    assert handler.seen[0]["arguments"] == {"path": "a.md", "text": "hello"}


async def _approve(approvals: InMemoryApprovalStore, key: str) -> None:
    await approvals.decide(key, ApprovalStatus.APPROVED, decided_by="operator")


def test_a_suspended_record_without_a_batch_is_refused() -> None:
    """An older record (or a corrupted one) must not be reconstructed by guessing."""
    store = InMemoryPersistence()
    record = RunRecord(
        run_id="r1",
        namespace=NAMESPACE,
        session_id="s1",
        status=RunStatus.SUSPENDED,
        attempt_id="a1",
        pending_interaction={"kind": "approval", "call_id": "call-1"},
        updated_at=1.0,
    )
    run(store.save_run("r1", record.to_record(), expected_version=None))
    harness = DefaultHarness(stream_fn=Script(final_message()), persistence=store, system_prompt="be brief")
    with pytest.raises(ContractError) as excinfo:
        run(harness.resume("r1", {}, context(attempt="a2")))
    assert "no recorded tool batch" in str(excinfo.value)


def test_state_and_durability_are_visible_for_a_suspended_run() -> None:
    deployment = first_turn_only(Calls())
    run(deployment.harness.start(RunRequest(prompt="write it"), context()))
    state = run(deployment.harness.state("r1"))
    assert state.status is RunStatus.SUSPENDED
    assert state.pending_interaction is not None
    assert deployment.harness.durability is Durability.VOLATILE
    assert InvocationStatus.AWAITING_APPROVAL.value == "awaiting_approval"
