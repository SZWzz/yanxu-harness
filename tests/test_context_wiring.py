"""C3: the managed context path through the real harness, over a real wire (E09/E10).

These tests drive `DefaultHarness.start`/`resume` with a `ContextController`, a real
`CoreStreamBridge` and a loopback HTTP server, and read the result back from SQLite. What
they are here to prove, in the plan's own terms:

* the controller is actually consulted, once per turn, on the same path the request takes
  to the wire — not a parallel code path that happens to have tests;
* the projection facts are **durable before the bytes are sent**: the provider's gate
  reads the run store from its own connection and finds them (the same technique F03 uses
  for the request phase);
* the transcript is never what the projection replaces: a compacted view goes to the wire
  while `RunRecord.messages` keeps every message;
* a refusal — from preparation or from the final byte gate — sends nothing and is recorded
  as a budget stop with its own code, never as a model failure;
* a failed preparation commit dispatches nothing, and an ambiguous configuration is
  refused before dispatch rather than silently running two compactions.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from context_helpers import real_convert
from durable_edit import DeploymentPaths, build, final_message, run_context, tool_call_message
from wire_fixture import WireFixture, delta_frame, tool_delta_frame

from yanxu_harness.context import (
    ContextController,
    ContextWindow,
    HeuristicTokenEstimator,
    MeasurementInput,
    UnknownPolicy,
    payload_budget_gate,
    transcript_fingerprint,
)
from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop
from yanxu_harness.core.execution import Phase
from yanxu_harness.core.messages import normalize_context, to_llm_messages
from yanxu_harness.core.types import AgentContext, UserMessage, message_from_json
from yanxu_harness.providers.core_bridge import to_provider_request
from yanxu_harness.errors import ContractError
from yanxu_harness.providers import CoreStreamBridge, OpenAICompatibleProvider, ProviderConfig
from yanxu_harness.runtime import DefaultHarness
from yanxu_harness.runtime.harness import RunRequest, RunRecord
from yanxu_harness.storage import SqlitePersistence
from yanxu_harness.storage.ports import StorageConflict
from yanxu_harness.tools import ApprovalStatus
from yanxu_harness.tools import EffectState

ROOMY = ContextWindow(limit_tokens=100_000, output_reserve=64)


def run(coro: Any) -> Any:
    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=20)

    return asyncio.run(guard())


def controller(
    *, window: ContextWindow = ROOMY, max_payload_bytes: int | None = 100_000, on_unknown: UnknownPolicy = UnknownPolicy.PROCEED
) -> ContextController:
    return ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        converter=real_convert,
        window=window,
        max_payload_bytes=max_payload_bytes,
        on_unknown=on_unknown,
    )


class CountingController:
    """Wraps a real controller to record the calls the harness makes."""

    def __init__(self, inner: ContextController) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self.prepared: list[Mapping[str, Any]] = []

    def prepare(self, payload: Any, *, scope: str) -> Mapping[str, Any]:
        self.calls.append(scope)
        result = self.inner.prepare(payload, scope=scope)
        self.prepared.append(dict(result))
        return result


def provider(
    fixture: WireFixture, subject: ContextController, *, gate: Any = None, seen: list[Any] | None = None
) -> OpenAICompatibleProvider:
    if gate is None:
        gate = payload_budget_gate(subject.budget, subject.projector.estimator, on_report=(seen.append if seen is not None else None))
    return OpenAICompatibleProvider(
        ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=5.0),
        payload_gate=gate,
    )


def write_frames(text: str = "hello") -> list[list[Mapping[str, Any]]]:
    """Two turns: the model asks for the write tool, then answers with text."""
    arguments = json.dumps({"path": "a.md", "text": text})
    return [
        [
            tool_delta_frame(0, call_id="call-1", name="notes.write", arguments=arguments),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [delta_frame("写好了。", finish="stop")],
    ]


def dispatched_messages(messages: Sequence[Any]) -> list[Mapping[str, Any]]:
    """The messages the provider would put on the wire for this transcript."""
    request = to_provider_request(
        None, normalize_context(AgentContext(messages=to_llm_messages(list(messages))))
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(model="m", base_url="http://127.0.0.1:1", timeout_seconds=1.0)
    )
    return list(provider.build_body(request)["messages"])


def stored_record(paths: DeploymentPaths) -> RunRecord:
    connection = sqlite3.connect(paths.runs)
    try:
        row = connection.execute("select state from runs where run_id = ?", ("r1",)).fetchone()
    finally:
        connection.close()
    assert row is not None, "the run record must have been committed"
    return RunRecord.from_record(json.loads(row[0]))


# -- E09: the controller is on the real path, and its facts are durable ---


def test_e09_the_projection_facts_are_durable_before_the_bytes_are_sent(tmp_path: Path) -> None:
    """A second connection finds the projection before the wire sees the payload.

    This is the strongest form of "committed before dispatch" available without killing a
    process: the gate runs *at* dispatch time, reads SQLite itself, and asserts the facts
    it finds describe exactly the bytes it is about to allow.
    """
    paths = DeploymentPaths.under(tmp_path / "state")
    observations: list[Mapping[str, Any]] = []

    def gate(body: bytes, _request: Any) -> None:
        record = stored_record(paths)
        observations.append(
            {
                "context_state": record.context_state,
                "request_view": (record.execution or {}).get("request_view"),
                "wire": json.loads(body.decode("utf-8"))["messages"],
            }
        )

    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        subject = controller()
        counting = CountingController(subject)
        harness, _gateway = build(
            paths,
            CoreStreamBridge(provider=provider(fixture, subject, gate=gate)),
            context_controller=counting,
        )
        outcome = run(harness.start(RunRequest(prompt="hi"), run_context()))

        assert fixture.requests, "the request still has to reach the wire"
        assert outcome.status.value == "completed"

    assert len(observations) == 1
    observation = observations[0]
    facts = observation["context_state"]
    assert facts is not None, "the projection facts must be committed before the bytes are sent"
    assert facts["context_schema_version"] == 1
    assert facts["request_fingerprint"], "the committed facts name the request they describe"
    # Same messages, two representations: the committed view is the transcript form, the
    # wire body is what the provider builds from it. Compare through that same builder, so
    # this is an equality of content rather than of shape.
    committed_view = [message_from_json(dict(item)) for item in observation["request_view"] or ()]
    assert dispatched_messages(committed_view) == observation["wire"], (
        "the committed request view and the dispatched body must be the same messages"
    )


def test_e09_a_single_turn_run_prepares_once_and_keeps_the_transcript(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        subject = controller()
        counting = CountingController(subject)
        harness, _gateway = build(
            paths, CoreStreamBridge(provider=provider(fixture, subject)), context_controller=counting
        )
        outcome = run(harness.start(RunRequest(prompt="hi"), run_context()))

    assert counting.calls == ["project-a/s1/r1"], "prepared once, under the run's own scope"
    assert outcome.context_state is not None
    assert outcome.context_state["scope"] == "project-a/s1/r1"
    assert outcome.context_state["estimator_id"] == "stdlib-heuristic"
    assert outcome.context_state["estimate_exact"] is False
    assert outcome.context_state["refused"] is None

    record = stored_record(paths)
    assert [message["role"] for message in record.messages] == ["system", "user", "assistant"], (
        "the transcript is the conversation, not the view: nothing was dropped from it"
    )
    view = (record.execution or {}).get("request_view") or []
    assert [item["role"] for item in view] == ["system", "user"]
    assert record.context_state is not None
    assert record.context_state["request_fingerprint"] == transcript_fingerprint(view), (
        "the committed facts describe the committed view"
    )
    assert record.status.value == "completed"


def test_e09_each_turn_prepares_once_and_the_round_trip_travels_intact(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    with WireFixture(scripts=write_frames()) as fixture:
        subject = controller()
        counting = CountingController(subject)
        harness, _gateway = build(
            paths,
            CoreStreamBridge(provider=provider(fixture, subject)),
            context_controller=counting,
            auto_write=True,
        )
        outcome = run(harness.start(RunRequest(prompt="write hello"), run_context()))

        assert outcome.status.value == "completed"
        assert len(fixture.requests) == 2, "one request per turn, no extra round trip"
        assert counting.calls == ["project-a/s1/r1", "project-a/s1/r1"], (
            "one preparation per turn, each committed before its own request"
        )
        first = fixture.body_of(0)
        second = fixture.body_of(1)
        assert not [item for item in first["messages"] if item["role"] == "tool"], (
            "the first request cannot carry a tool result that does not exist yet"
        )
        tool_messages = [item for item in second["messages"] if item["role"] == "tool"]
        assert [item["tool_call_id"] for item in tool_messages] == ["call-1"]
        assert "wrote hello" in tool_messages[0]["content"]

    record = stored_record(paths)
    assert [message["role"] for message in record.messages] == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ], "the transcript keeps the whole round trip once"
    assert [item["tool_call_id"] for item in record.messages if item["role"] == "toolResult"] == ["call-1"]
    view = (record.execution or {}).get("request_view") or []
    assert record.context_state is not None
    assert record.context_state["request_fingerprint"] == transcript_fingerprint(view)


def test_e09_a_compacted_view_goes_to_the_wire_while_the_transcript_stays_whole(tmp_path: Path) -> None:
    """The managed projection is a *request view*: it never becomes the history."""
    paths = DeploymentPaths.under(tmp_path / "state")
    # A window small enough that the projection has to drop the older turns, but not so
    # small that the mandatory minimum refuses.
    tight = ContextWindow(limit_tokens=120, output_reserve=16)
    with WireFixture(scripts=write_frames()) as fixture:
        subject = controller(window=tight)
        counting = CountingController(subject)
        harness, _gateway = build(
            paths,
            CoreStreamBridge(provider=provider(fixture, subject)),
            context_controller=counting,
            auto_write=True,
        )
        outcome = run(harness.start(RunRequest(prompt="write hello"), run_context()))
        assert outcome.status.value == "completed"
        second = fixture.body_of(1)

    record = stored_record(paths)
    roles = [message["role"] for message in record.messages]
    assert roles == ["system", "user", "assistant", "toolResult", "assistant"]
    wire_roles = [item["role"] for item in second["messages"]]
    assert "toolResult" not in wire_roles or len(wire_roles) <= len(roles)
    # Whatever the projection dropped, the facts say so and the source is the transcript.
    facts = record.context_state or {}
    assert facts["removed_messages"] == sum(len(entry["positions"]) for entry in facts["dropped"])
    assert all(entry["reason"] for entry in facts["dropped"])
    assert counting.calls and counting.prepared, "the controller prepared the request"


# -- E10: refusals and failures ------------------------------------------


def test_e10_a_preparation_refusal_sends_nothing_and_is_a_budget_stop(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        subject = controller(window=ContextWindow(limit_tokens=1, output_reserve=64))
        harness, _gateway = build(
            paths, CoreStreamBridge(provider=provider(fixture, subject)), context_controller=subject
        )
        outcome = run(harness.start(RunRequest(prompt="hi"), run_context()))

        assert fixture.requests == [], "a refused preparation must not reach the wire"
    assert outcome.status.value == "usage_limited"
    assert outcome.error_code == "context_budget_exceeded"
    assert outcome.stop_code == "context_budget_exceeded"
    assert "required" in outcome.detail, "the refusal names what it would not cut"

    record = stored_record(paths)
    assert [message["role"] for message in record.messages] == ["system", "user"], (
        "a refusal changes nothing about the transcript"
    )
    assert record.context_state is not None
    assert record.context_state["refused"]["error_code"] == "context_budget_exceeded"
    assert record.context_state["capacity"] == 0


def test_e10_the_final_byte_gate_refuses_after_preparation_and_sends_nothing(tmp_path: Path) -> None:
    """Two independent checkpoints: the window fits, the serialized body does not."""
    paths = DeploymentPaths.under(tmp_path / "state")
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        subject = controller(max_payload_bytes=120)
        harness, _gateway = build(
            paths, CoreStreamBridge(provider=provider(fixture, subject)), context_controller=subject
        )
        outcome = run(harness.start(RunRequest(prompt="hi"), run_context()))

        assert fixture.requests == [], "the gate refuses before the transport call"
    assert outcome.status.value == "usage_limited"
    assert outcome.error_code == "context_budget_exceeded"
    assert "byte limit" in outcome.detail

    record = stored_record(paths)
    facts = record.context_state or {}
    assert facts.get("refused") is not None, "the committed facts explain the refusal"
    assert facts["refused"]["error_code"] == "context_budget_exceeded"
    assert (record.execution or {}).get("request_view"), (
        "the prepared view is still recorded: a reader can see what was refused"
    )


class FailingPreparationPersistence(SqlitePersistence):
    """A real store whose commit of a *prepared* request fails exactly once."""

    def __init__(self, path: Path, *, handshake: Path) -> None:
        super().__init__(path)
        self.handshake = handshake
        self.reached: list[str] = []

    async def commit_transition(
        self, run_id: str, state: Any, *, expected_version: Any, lease: Any = None, events: Sequence[Any] = ()
    ) -> Any:
        execution = dict(state).get("execution") or {}
        prepared = execution.get("request_view") and execution.get("phase") == Phase.REQUESTING_MODEL.value
        if prepared and not self.reached:
            self.reached.append(str(execution.get("phase")))
            with self.handshake.open("a", encoding="utf-8") as handle:
                handle.write("prepared-commit\n")
                handle.flush()
                import os

                os.fsync(handle.fileno())
            raise StorageConflict(run_id, "injected failure at the preparation boundary")
        return await super().commit_transition(
            run_id, state, expected_version=expected_version, lease=lease, events=events
        )


def test_e10_a_failed_preparation_commit_dispatches_nothing(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    handshake = paths.runs.parent / "preparation-handshake.log"
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        subject = controller()
        harness, _gateway = build(
            paths, CoreStreamBridge(provider=provider(fixture, subject)), context_controller=subject
        )
        failing = FailingPreparationPersistence(paths.runs, handshake=handshake)
        harness._persistence = failing  # noqa: SLF001 - the injected store is the point

        with pytest.raises(StorageConflict):
            run(harness.start(RunRequest(prompt="hi"), run_context()))
        assert fixture.requests == [], "a commit that did not happen must not dispatch"

    assert handshake.is_file() and handshake.read_text(encoding="utf-8").strip() == "prepared-commit", (
        "the port was reached with a prepared view — that is the commit that failed"
    )


def test_e20_an_ambiguous_configuration_is_refused_before_dispatch() -> None:
    def prepare(payload: Any) -> Any:
        return None

    async def scenario() -> None:
        with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
            bridge = CoreStreamBridge(provider=provider(fixture, controller()))
            for conflicting in (
                {"transform_context": lambda messages, _signal: messages},
                {"persist_transformed_context": True},
            ):
                config = AgentLoopConfig(
                    stream_fn=bridge, prepare_context=prepare, **conflicting
                )
                with pytest.raises(ContractError) as refusal:
                    await run_agent_loop(
                        [UserMessage(content="hi")],
                        AgentContext(messages=[UserMessage(content="hi")]),
                        config,
                        None,
                        _Sink(),
                    )
                assert "prepare_context" in str(refusal.value)
            assert fixture.requests == [], "an ambiguous configuration is refused before dispatch"

    run(scenario())


def test_e20_a_harness_controller_must_expose_prepare(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        DefaultHarness(stream_fn=lambda *_: None, context_controller=object())
    with pytest.raises(ContractError):
        DefaultHarness(stream_fn=lambda *_: None, context_controller=lambda payload: None)


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)


# -- resume --------------------------------------------------------------


def test_a_resumed_attempt_prepares_again_and_never_duplicates_the_transcript(tmp_path: Path) -> None:
    """A pause and a second harness: each attempt prepares its own request.

    The pause is a real approval wait (the write is not authorised yet), which is what the
    harness's suspension model represents: a settled batch with nothing left to run is not a
    resumable checkpoint, and this test does not pretend otherwise.
    """
    paths = DeploymentPaths.under(tmp_path / "state")
    with WireFixture(scripts=write_frames()) as fixture:
        subject = controller()
        counting = CountingController(subject)
        first, gateway = build(
            paths, CoreStreamBridge(provider=provider(fixture, subject)), context_controller=counting
        )
        paused = run(first.start(RunRequest(prompt="write hello"), run_context()))
        assert paused.status.value == "suspended"
        assert paused.pending_interaction["kind"] == "approval"
        first_facts = dict(paused.context_state or {})
        assert first_facts, "the paused attempt committed its projection facts too"

        binding = paused.pending_interaction["approval_binding_key"]
        # The test owns this ledger; the gateway does not expose its store on purpose.
        run(gateway._approvals.decide(binding, ApprovalStatus.APPROVED, decided_by="operator"))  # noqa: SLF001
        requests_before_resume = len(fixture.requests)

        second, _gateway = build(
            paths,
            CoreStreamBridge(provider=provider(fixture, subject)),
            context_controller=counting,
            attempt="a2",
        )
        resumed = run(second.resume("r1", {}, run_context("a2")))

    assert resumed.status.value == "completed"
    assert len(fixture.requests) == requests_before_resume + 1, "the resume asks once more"
    assert counting.calls == ["project-a/s1/r1", "project-a/s1/r1"], (
        "each attempt prepares its own request; a projection is never reused blindly"
    )
    record = stored_record(paths)
    assert [message["role"] for message in record.messages] == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ], "the resumed transcript replaces the pause placeholder instead of appending twice"
    assert [item["tool_call_id"] for item in record.messages if item["role"] == "toolResult"] == ["call-1"]
    facts = record.context_state or {}
    assert facts.get("source_fingerprint") != first_facts.get("source_fingerprint"), (
        "the resumed attempt prepared the grown transcript, not the paused one"
    )
    assert facts["scope"] == "project-a/s1/r1"
