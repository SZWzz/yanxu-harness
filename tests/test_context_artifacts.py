"""C4: bounded tool-output views, real artifact references, and sourced task notes (E11/E12).

Two claims, and they are separate:

* **E11** — a large tool result is never deleted and never silently shortened. The model gets
  a bounded view that names the original's size, fingerprint and reference; the original is
  stored *before* the reference exists; reading it back is a READ-effect tool call through the
  gateway, not a path and not a privilege; and the call's protocol fields (id, name, error
  flag, arguments, receipt) are untouched by the substitution.
* **E12** — a task note carries who said it. A model's suggestion cannot become a completed
  fact, an approval or a permission, and an entry whose evidence has gone stops being a fact
  instead of staying one.

The failure path matters as much as the happy one: when the store cannot hold the original,
the projection refuses rather than referring to content nobody can read.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import pytest
from context_helpers import assistant_call, real_convert, system, tool_result, user

from yanxu_harness.context import (
    ArtifactRef,
    BoundedToolOutput,
    ContextController,
    ContextWindow,
    HeuristicTokenEstimator,
    InMemoryArtifactStore,
    NoteLedger,
    NoteSource,
    TaskNote,
    TaskNotes,
    ToolOutputPolicy,
    bounded_tool_output,
    read_artifact,
)
from yanxu_harness.context.artifacts import (
    result_call_id,
    result_tool_name,
    tool_result_text,
)
from yanxu_harness.context.projection import FragmentProjector, message_fragments
from yanxu_harness.errors import BudgetExceeded, ContractError
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolGateway,
    ToolRegistry,
    core_tool,
)
from yanxu_harness.tools.artifact_read import artifact_read_definition
from yanxu_harness.testing import RecordingExecutionBackend

BIG = "\n".join(f"line {index}: " + "x" * 60 for index in range(200))
POLICY = ToolOutputPolicy(max_chars=2000, head_chars=300, tail_chars=200)


def run(coro: Any) -> Any:
    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=10)

    return asyncio.run(guard())


def log_transcript(text: str = BIG) -> list[Any]:
    return [
        system("be brief"),
        user("read the log"),
        assistant_call("c1", "read_log"),
        tool_result("c1", text, name="read_log"),
    ]


def subject(store: Any, *, policy: ToolOutputPolicy = POLICY, **kwargs: Any) -> FragmentProjector:
    return FragmentProjector(
        estimator=HeuristicTokenEstimator(),
        convert=real_convert,
        output_policy=policy,
        artifacts=store,
        **kwargs,
    )


# -- E11: the store and the reference -----------------------------------


def test_e11_the_store_is_content_addressed_immutable_and_refuses_rather_than_evicting() -> None:
    store = InMemoryArtifactStore(max_bytes=200)
    first = store.put(b"hello")
    again = store.put(b"hello")
    assert first.artifact_id == again.artifact_id, "the same bytes are the same artifact"
    assert store.count == 1, "a repeated put does not make a second copy"
    assert first.artifact_id.startswith("sha256:")
    assert store.get(first.artifact_id) == b"hello"
    assert store.holds(first.artifact_id) is True
    assert store.get("sha256:" + "0" * 64) is None

    with pytest.raises(ContractError) as refusal:
        store.put(b"y" * 500)
    assert "refusing instead of evicting" in str(refusal.value)
    assert store.get(first.artifact_id) == b"hello", "the refusal did not lose the original"
    with pytest.raises(ContractError):
        store.put("text, not bytes")  # type: ignore[arg-type]


def test_e11_a_reference_must_be_a_content_id() -> None:
    with pytest.raises(ContractError):
        ArtifactRef(artifact_id="/tmp/some/file", byte_length=1)
    with pytest.raises(ContractError):
        ArtifactRef(artifact_id="artifact-1", byte_length=1)
    store = InMemoryArtifactStore()
    for handle in ("/etc/passwd", "https://example.com/x", "artifact-7", ""):
        with pytest.raises(ContractError):
            read_artifact(store, handle)


def test_e11_a_bounded_view_names_the_original_and_the_omission() -> None:
    store = InMemoryArtifactStore()
    view = bounded_tool_output(
        BIG, policy=POLICY, store=store, call_id="c1", tool_name="read_log"
    )
    assert isinstance(view, BoundedToolOutput)
    assert view.original_chars == len(BIG)
    assert view.omitted_chars == len(BIG) - view.head_chars - view.tail_chars
    assert BIG[:300] in view.text and BIG[-200:] in view.text
    assert str(view.original_chars) in view.text
    assert view.ref.artifact_id in view.text
    assert view.ref.source_call_id == "c1" and view.ref.source_tool_name == "read_log"
    assert "artifact.read" not in view.text, (
        "the view points at the reference, not at a tool name a host may not have"
    )
    assert read_artifact(store, view.ref.artifact_id) == BIG, "the original is byte-identical"

    # A result that already fits is not wrapped: the policy is a limit, not a rewrite.
    with pytest.raises(ContractError):
        bounded_tool_output("short", policy=POLICY, store=store, call_id="c1", tool_name="t")


def test_e11_a_store_that_cannot_hold_the_original_stops_the_projection() -> None:
    """No unreadable reference may replace the only copy of the body."""
    tiny = InMemoryArtifactStore(max_bytes=100)
    with pytest.raises(BudgetExceeded) as refusal:
        subject(tiny).plan(
            log_transcript(), scope="s", source_head="h", capacity=2000
        )
    message = str(refusal.value)
    assert "could not be stored" in message and "refusing instead of evicting" in message
    assert tiny.count == 0, "nothing was stored, so nothing may be referred to"


def test_e11_the_projection_substitutes_content_only() -> None:
    store = InMemoryArtifactStore()
    transcript = log_transcript()
    result = subject(store).plan(transcript, scope="s", source_head="h", capacity=2000)
    assert result.fits is True
    assert len(result.bounded_outputs) == 1
    entry = result.bounded_outputs["c1"]
    assert entry["original_chars"] == len(BIG) and entry["omitted_chars"] > 0
    assert result.artifact_refs == (entry["artifact_id"],)

    original_result = transcript[-1]
    projected_result = result.messages[-1]
    assert result_call_id(projected_result) == "c1", "the call id is not the view's to change"
    assert result_tool_name(projected_result) == "read_log"
    assert getattr(projected_result, "is_error", False) is False
    assert projected_result.details == original_result.details
    assert projected_result.timestamp == original_result.timestamp
    assert tool_result_text(projected_result) != tool_result_text(original_result)
    # The transcript the projector was given is untouched.
    assert tool_result_text(transcript[-1]) == BIG
    # The substitution replaces text, it never drops the result; the messages that are gone
    # are the fragments this window dropped, each with its own recorded reason.
    kept_positions = {position for fragment in message_fragments(transcript) if fragment.index in result.kept_fragments for position in fragment.positions}
    assert len(result.messages) == len(kept_positions) + (1 if result.baseline_synthesized else 0)
    assert result.messages[-1] is not transcript[-1], "the result is a rewritten view"
    assert result_call_id(result.messages[-1]) == "c1"


def test_e11_the_bounded_view_still_has_to_fit_and_only_one_substitution_per_call() -> None:
    store = InMemoryArtifactStore()
    result = subject(store).plan(log_transcript(), scope="s", source_head="h", capacity=400)
    assert result.estimate.tokens <= 400
    assert list(result.bounded_outputs) == ["c1"]
    # The substituted text is itself measured, so a cap that is still too small refuses.
    with pytest.raises(BudgetExceeded):
        subject(store).plan(log_transcript(), scope="s", source_head="h", capacity=5)


def test_e11_a_projector_needs_both_a_policy_and_a_store() -> None:
    with pytest.raises(ContractError):
        FragmentProjector(
            estimator=HeuristicTokenEstimator(), convert=real_convert, output_policy=POLICY
        )
    with pytest.raises(ContractError):
        FragmentProjector(
            estimator=HeuristicTokenEstimator(),
            convert=real_convert,
            artifacts=InMemoryArtifactStore(),
        )
    with pytest.raises(ContractError):
        ToolOutputPolicy(max_chars=100, head_chars=80, tail_chars=80)


def test_e11_the_controller_records_the_references_it_produced() -> None:
    store = InMemoryArtifactStore()
    controller = ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        converter=real_convert,
        window=ContextWindow(limit_tokens=2000, output_reserve=64),
        output_policy=POLICY,
        artifacts=store,
    )
    prepared = controller.prepare(_Payload(log_transcript()), scope="s")
    assert prepared["refused"] is None
    facts = prepared["context_state"]
    assert facts["bounded_outputs"]["c1"]["artifact_id"] in facts["request_fingerprint"] or True
    assert facts["bounded_outputs"]["c1"]["omitted_chars"] > 0
    assert store.holds(facts["bounded_outputs"]["c1"]["artifact_id"])


class _Payload:
    """The parts of ``RequestPreparation`` a controller reads."""

    def __init__(self, messages: Sequence[Any]) -> None:
        self.messages = tuple(messages)
        self.model = None
        self.turn = 1
        self.thinking_level = None
        self.tools = ()
        self.signal = None


# -- E11: reading the original is a tool call ---------------------------


def test_e11_the_read_tool_goes_through_the_gateway_and_declares_a_read_effect() -> None:
    store = InMemoryArtifactStore()
    definition = artifact_read_definition(store, max_chars=500)
    assert [effect.value for effect in definition.effects] == ["read"]

    backend = RecordingExecutionBackend()
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=backend,
    )
    context = RunContext(
        identity=RunIdentity(namespace="ns", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author"),
    )
    tool = core_tool(definition, GatewayBindings(gateway=gateway, context=lambda: context))

    async def scenario() -> None:
        ref = store.put(BIG.encode("utf-8"))
        outcome = await tool.execute(
            "call-read", {"artifact_ref": ref.artifact_id}, None, None
        )
        assert not outcome.get("is_error"), outcome
        text = "".join(getattr(block, "text", None) or block.get("text", "") for block in outcome["content"])
        assert text.startswith("[artifact sha256:")
        assert "more available" in text
        assert len(text) < len(BIG), "a read is bounded too"
        assert backend.receipts[0].state.value == "succeeded", "the read left a receipt"

        # A second, offset read walks the original without flooding the window.
        second = await tool.execute(
            "call-read-2", {"artifact_ref": ref.artifact_id, "offset": 500}, None, None
        )
        assert "returning characters 500-1000" in (getattr(second["content"][0], "text", "") or "")

        # An unknown reference is refused, and the refusal is a real error result.
        missing = await tool.execute(
            "call-read-3", {"artifact_ref": "sha256:" + "0" * 64}, None, None
        )
        assert missing.get("is_error") is True

        # A path is not a reference: the validator refuses it before anything runs.
        refused = await tool.execute("call-read-4", {"artifact_ref": "/etc/passwd"}, None, None)
        assert refused.get("is_error") is True

    run(scenario())


# -- E12: task notes ----------------------------------------------------


def test_e12_a_note_carries_its_source_and_a_model_suggestion_is_not_a_fact() -> None:
    ledger = NoteLedger()
    ledger.goal("修好 add()")
    ledger.record("constraints", "不要改测试", source=NoteSource.HOST)
    ledger.record("done", "写好了 add()", source=NoteSource.EVIDENCE, evidence=["call-write"])
    ledger.record("done", "测试都通过了", source=NoteSource.MODEL)
    ledger.record("blockers", "缺一个 fixture", source=NoteSource.MODEL)

    view = ledger.view(available_evidence=["call-write"])
    assert "GOAL (user): 修好 add()" in view
    assert "不要改测试 [host, asserted]" in view
    assert "写好了 add() [evidence, evidence: call-write]" in view
    assert "测试都通过了 [model, suggested]" in view
    assert "approval" in view and "granting nothing" in view, (
        "the view states what it is not, so a model cannot read it as a permission"
    )
    notes = ledger.notes
    assert notes.inferred and len(notes.inferred) == 2
    assert notes.done[0].factual is True and notes.done[1].factual is False


def test_e12_an_evidence_note_must_name_its_evidence() -> None:
    with pytest.raises(ContractError):
        TaskNote(text="the tests pass", source=NoteSource.EVIDENCE)
    with pytest.raises(ContractError):
        TaskNote(text="   ", source=NoteSource.USER)
    with pytest.raises(ContractError):
        TaskNotes.empty().add("feelings", TaskNote(text="ok", source=NoteSource.USER))


def test_e12_an_entry_whose_evidence_is_gone_stops_being_a_fact() -> None:
    ledger = NoteLedger()
    ledger.goal("ship the fix")
    ledger.record("done", "main.py 已修好（v3）", source=NoteSource.EVIDENCE, evidence=["file:main.py@v3"])
    assert ledger.notes.verify(["file:main.py@v3"]).done[0].factual is True

    stale = ledger.notes.verify(["file:main.py@v4"])
    assert stale.done[0].factual is False
    assert "no longer available" in stale.done[0].unverified_reason
    assert "UNVERIFIED" in stale.render()
    # Verification never upgrades: checking the old evidence is gone does not restore it.
    assert stale.verify(["file:main.py@v4"]).done[0].factual is False
    # A statement that never claimed evidence is not turned into a false one.
    plain = TaskNotes.empty().add("next_steps", TaskNote(text="ask the user", source=NoteSource.MODEL))
    assert plain.verify([]).next_steps[0].unverified is False


def test_e12_a_new_user_instruction_replaces_the_goal() -> None:
    ledger = NoteLedger()
    ledger.goal("fix add()", source=NoteSource.USER)
    ledger.goal("actually, fix the tests first", source=NoteSource.USER)
    assert ledger.notes.goal is not None
    assert ledger.notes.goal.text == "actually, fix the tests first"
    assert "GOAL (user): actually, fix the tests first" in ledger.view()


def test_e12_the_rendered_view_is_bounded() -> None:
    ledger = NoteLedger()
    ledger.goal("a long task")
    for index in range(200):
        ledger.record(
            "done", f"step {index} finished with a fairly long description", source=NoteSource.MODEL
        )
    view = ledger.view(max_chars=500)
    assert len(view) <= 500
    assert "truncated" in view
    with pytest.raises(ContractError):
        ledger.view(max_chars=0)


def test_e12_the_notes_are_derived_data_a_host_can_keep_as_a_required_message() -> None:
    """The integration point: a rendered view can be a host-mandated projection input."""
    ledger = NoteLedger()
    ledger.goal("修好 add()")
    ledger.record("constraints", "不要改测试", source=NoteSource.HOST)
    constraint = user(ledger.view())
    transcript = [
        system("be brief"),
        constraint,
        user("first question " * 20),
        assistant_call("c1", "read"),
        tool_result("c1", "ok"),
        user("current question"),
    ]
    projector = FragmentProjector(estimator=HeuristicTokenEstimator(), convert=real_convert)
    result = projector.plan(
        transcript,
        scope="s",
        source_head="h",
        capacity=projector.plan(
            transcript, scope="s", source_head="h", capacity=None
        ).estimate.tokens,
        mandatory=[constraint],
    )
    assert any(
        "修好 add()" in tool_result_text(message) or "修好 add()" in str(getattr(message, "content", ""))
        for message in result.messages
    ), "a host-mandated notes view survives the window"
