"""C6: the managed context across process boundaries, schemas and scopes.

Five claims about what a *restart* (or a second deployment) can and cannot do with the
state this round added:

* **a crash inside a prepared request leaves the view and its facts readable** — a real
  child dies inside the model request after the preparation commit, and the parent reads
  `execution.request_view`, `context_state` and `phase=requesting_model` from SQLite;
* **a committed reference outlives the process that wrote it** — with a durable artifact
  store, the dying child's bounded tool result is read back byte-for-byte by the parent;
* **an unknown projection schema is refused, not guessed** — the versioned
  `context_schema_version` is what makes last round's records readable and this round's
  future records safe;
* **a volatile store cannot be the home of a committed reference** when the host says
  references must survive, and the refusal happens before anything is written;
* **a cache never crosses a scope**, and a committed fact carries no message text, no key
  and no raw output.

The long-conversation fixture and the evaluation record are separate (`docs/verification.md`,
"not verified"): this file is about recovery and isolation, not about answer quality.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from child_process import child_json, run_child
from context_helpers import assistant_call, real_convert, system, tool_result, user
from durable_edit import DeploymentPaths

from yanxu_harness.context import (
    ContextController,
    ContextWindow,
    HeuristicTokenEstimator,
    InMemoryArtifactStore,
    SummaryBudget,
    SummaryCompactor,
    SummaryResult,
    ToolOutputPolicy,
    bounded_tool_output,
    read_artifact,
    transcript_fingerprint,
)
from yanxu_harness.context.projection import FragmentProjector
from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import SqliteArtifactStore, SqlitePersistence
from yanxu_harness.storage.artifacts import ARTIFACT_SCHEMA_VERSION

TIGHT = ContextWindow(limit_tokens=600, output_reserve=64)


def run(coro: Any) -> Any:
    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guard())


def load_record(paths: DeploymentPaths) -> RunRecord:
    connection = sqlite3.connect(paths.runs)
    try:
        row = connection.execute("select state from runs where run_id = ?", ("r1",)).fetchone()
    finally:
        connection.close()
    assert row is not None, "the run record must exist after the crash"
    return RunRecord.from_record(json.loads(row[0]))


# -- the crash windows ---------------------------------------------------


def test_c6_a_crash_inside_a_prepared_request_leaves_the_view_and_its_facts(tmp_path: Path) -> None:
    """The C3/C5 fields, read by another process after a real `os._exit`."""
    directory = tmp_path / "state"
    paths = DeploymentPaths.under(directory)
    completed = run_child(
        ["harness-prepare-then-die", str(directory), str(directory / "marker"), str(paths.counter)]
    )
    assert completed.returncode == 9, completed.stderr
    assert child_json(completed)["status"] == "died"

    record = load_record(paths)
    assert record.status.value == "running"
    execution = record.execution or {}
    assert execution["phase"] == "requesting_model"
    view = execution["request_view"]
    assert [message["role"] for message in view] == ["system", "user"], (
        "the prepared request view is committed before the request is dispatched"
    )
    facts = record.context_state or {}
    assert facts["context_schema_version"] == 1
    assert facts["source_head"] == transcript_fingerprint(
        [__import__("yanxu_harness.core.types", fromlist=["message_from_json"]).message_from_json(item) for item in view]
    ), "the facts name the transcript they were derived from"
    assert facts["capacity"] == 536 and facts["estimated_input_tokens"] is not None
    assert record.messages, "the transcript is committed too, and it is not the view"

    # A resumed driver can tell that a request was outstanding and that nothing ran.
    work = record.pending_work()
    assert work["model_request_outstanding"] is True
    assert work["batch_outcome_unknown"] is False
    assert work["request_prepared"] is True
    assert work["context_source_fingerprint"] == facts["source_fingerprint"]


def test_c6_a_committed_artifact_reference_is_resolvable_by_another_process(tmp_path: Path) -> None:
    """A bounded view refers to a stored original; the reference must outlive the writer."""
    directory = tmp_path / "state"
    paths = DeploymentPaths.under(directory)
    completed = run_child(
        ["harness-artifact-die", str(directory), str(directory / "marker"), str(paths.counter)]
    )
    assert completed.returncode == 9, completed.stderr

    record = load_record(paths)
    assert (record.execution or {})["phase"] == "requesting_model"
    facts = record.context_state or {}
    bounded = facts["bounded_outputs"]
    assert list(bounded) == ["call-1"], "the large tool result was bounded, not dropped"
    entry = bounded["call-1"]
    assert entry["original_chars"] == 14089 and entry["omitted_chars"] > 13000
    assert entry["store_durability"] == "durable"

    # The view the dying process committed carries the truncated text *and* the reference.
    view = [message for message in (record.execution or {})["request_view"] if message["role"] == "toolResult"]
    assert view, "the tool result is in the committed request view"
    text = view[0]["content"][0]["text"] if isinstance(view[0]["content"], list) else view[0]["content"]
    assert "tool result truncated" in text and entry["artifact_id"] in text

    # And the original is readable, byte-for-byte, from a store the parent opens itself.
    store = SqliteArtifactStore(directory / "artifacts.db")
    try:
        assert store.durability.value == "durable"
        original = read_artifact(store, entry["artifact_id"])
        assert len(original) == entry["original_chars"]
        assert original.startswith("line 0:") and original.endswith("x" * 60)
        assert store.ref(entry["artifact_id"]).source_call_id == "call-1"
    finally:
        store.close()


# -- schema versions -----------------------------------------------------


def test_c6_an_unknown_projection_schema_is_refused_not_guessed() -> None:
    from yanxu_harness.core.execution import context_state_from_record

    assert context_state_from_record(None) is None, "no projection facts is a valid state"
    facts = {"context_schema_version": 1, "scope": "s"}
    assert context_state_from_record(facts) == facts

    with pytest.raises(VersionRefused) as refusal:
        context_state_from_record({"context_schema_version": 2, "scope": "s"})
    assert "context state" in str(refusal.value)
    with pytest.raises(ContractError):
        context_state_from_record({"scope": "s"})
    with pytest.raises(ContractError):
        context_state_from_record(["not", "a", "mapping"])

    # The same refusal through the record a host actually reads.
    with pytest.raises(VersionRefused):
        RunRecord.from_record(
            {
                "schema_version": 1,
                "run_id": "r1",
                "namespace": "ns",
                "session_id": "s1",
                "status": "running",
                "context_state": {"context_schema_version": 99},
            }
        )


def test_c6_a_record_written_before_this_round_still_reads() -> None:
    """No `context_state` at all is the pre-round shape, and it stays readable."""
    record = RunRecord.from_record(
        {
            "schema_version": 1,
            "run_id": "r1",
            "namespace": "ns",
            "session_id": "s1",
            "status": "completed",
            "messages": [],
        }
    )
    assert record.context_state is None
    assert record.pending_work() == {}


def test_c6_the_artifact_store_refuses_a_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "artifacts.db"
    store = SqliteArtifactStore(path)
    store.put(b"hello")
    store.close()
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version={ARTIFACT_SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(ContractError) as refusal:
        SqliteArtifactStore(path)
    assert "newer build" in str(refusal.value)


# -- durability of references -------------------------------------------


def test_c6_a_volatile_store_cannot_hold_a_reference_a_host_requires_to_survive() -> None:
    policy = ToolOutputPolicy(max_chars=1000, head_chars=300, tail_chars=200, require_durable=True)
    with pytest.raises(ContractError) as refusal:
        bounded_tool_output(
            "x" * 5000,
            policy=policy,
            store=InMemoryArtifactStore(),
            call_id="c1",
            tool_name="read",
        )
    assert "would disappear" in str(refusal.value)
    assert "reference" in str(refusal.value)

    # The same policy against a durable store is fine and records it.
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        store = SqliteArtifactStore(Path(directory) / "a.db")
        try:
            view = bounded_tool_output(
                "x" * 5000, policy=policy, store=store, call_id="c1", tool_name="read"
            )
            assert view.store_durability == "durable"
            assert read_artifact(store, view.ref.artifact_id) == "x" * 5000
        finally:
            store.close()

    # A volatile store is still allowed when the host did not require durability, but the
    # committed record says which kind it was.
    permissive = bounded_tool_output(
        "x" * 5000,
        policy=ToolOutputPolicy(max_chars=1000, head_chars=300, tail_chars=200),
        store=InMemoryArtifactStore(),
        call_id="c1",
        tool_name="read",
    )
    assert permissive.store_durability == "volatile"


def test_c6_the_projector_refuses_to_bound_when_the_host_requires_durable_references() -> None:
    transcript = [
        system("be brief"),
        user("read the log"),
        assistant_call("c1", "read_log"),
        tool_result("c1", "y" * 20000, name="read_log"),
    ]
    projector = FragmentProjector(
        estimator=HeuristicTokenEstimator(),
        convert=real_convert,
        output_policy=ToolOutputPolicy(
            max_chars=1000, head_chars=300, tail_chars=200, require_durable=True
        ),
        artifacts=InMemoryArtifactStore(),
    )
    with pytest.raises(Exception) as refusal:
        projector.plan(transcript, scope="s", source_head="h", capacity=300)
    assert "refusing to refer" in str(refusal.value)


# -- cache isolation and diagnostics ------------------------------------


def test_c6_a_cache_never_crosses_a_scope() -> None:
    class Counting:
        def __init__(self) -> None:
            self.calls = 0

        async def summarize(self, messages: Sequence[Any], *, scope: str, source_head: str) -> SummaryResult:
            self.calls += 1
            return SummaryResult(text="摘要", source_head=source_head, scope=scope, usage={"input_tokens": 1})

    messages = [
        system("be brief", added=["read"]),
        user("question " * 60),
        assistant_call("c1", "read"),
        tool_result("c1", "result " * 60),
        user("newest question " * 60),
    ]
    summarizer = Counting()
    compactor = SummaryCompactor(
        projector=FragmentProjector(estimator=HeuristicTokenEstimator(), convert=real_convert),
        summarizer=summarizer,
        budget=SummaryBudget(max_calls=4),
    )

    async def scenario() -> None:
        first = await compactor.compact(messages, scope="ns-a/s1/r1", source_head="h", capacity=500)
        assert first.used_summary is True and summarizer.calls == 1
        same = await compactor.compact(messages, scope="ns-a/s1/r1", source_head="h", capacity=500)
        assert same.cache_hit is True and summarizer.calls == 1, "the same scope reuses its own answer"
        other = await compactor.compact(messages, scope="ns-b/s1/r1", source_head="h", capacity=500)
        assert other.cache_hit is False, "a different scope is a different cache"
        assert summarizer.calls == 2

    run(scenario())


def test_c6_the_committed_facts_carry_no_message_text_or_secret() -> None:
    """A public diagnostic describes the request; it does not quote the conversation."""
    secret = "sk-do-not-log-this-0123456789"
    key_phrase = "my private plan for chapter seven"
    # The large result is the *newest* fragment (the model is waiting on it), so the window
    # cannot fit it by dropping older turns: it must be bounded, which is the path whose
    # facts this test inspects.
    transcript = [
        system(f"you are a careful agent. token: {secret}"),
        user(key_phrase * 20),
        assistant_call("c1", "read_log"),
        tool_result("c1", "z" * 20000, name="read_log"),
    ]
    controller = ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        converter=real_convert,
        window=ContextWindow(limit_tokens=600, output_reserve=64),
        output_policy=ToolOutputPolicy(max_chars=1000, head_chars=300, tail_chars=200),
        artifacts=InMemoryArtifactStore(),
    )
    prepared = run(controller.prepare_async(_Payload(transcript), scope="ns/s1/r1"))
    facts = prepared["context_state"]
    serialized = json.dumps(facts, ensure_ascii=False)
    assert secret not in serialized, "a key must never reach a committed projection fact"
    assert key_phrase not in serialized, "the facts describe the request, they do not quote it"
    assert "z" * 40 not in serialized, "the bounded view's own body is not duplicated into the facts"
    # What the facts *do* carry is what a diagnosis needs.
    assert facts["bounded_outputs"]["c1"]["omitted_chars"] > 0
    assert facts["source_fingerprint"].startswith("sha256:")
    assert facts["estimator_id"] == "stdlib-heuristic"
    assert facts["capacity"] and facts["estimated_input_tokens"]


class _Payload:
    def __init__(self, messages: Sequence[Any]) -> None:
        self.messages = tuple(messages)
        self.model = None
        self.turn = 1
        self.thinking_level = None
        self.tools = ()
        self.signal = None


def test_c6_the_same_inputs_rebuild_the_same_projection_in_a_fresh_process_view() -> None:
    """Determinism across instances, which is what makes a stored projection checkable."""
    transcript = [
        system("be brief", added=["read"]),
        user("question " * 40),
        assistant_call("c1", "read"),
        tool_result("c1", "result " * 40),
        user("newest question"),
    ]
    first = run(
        ContextController.compose(
            estimator=HeuristicTokenEstimator(),
            converter=real_convert,
            window=TIGHT,
        ).prepare_async(_Payload(transcript), scope="ns/s1/r1")
    )
    second = run(
        ContextController.compose(
            estimator=HeuristicTokenEstimator(),
            converter=real_convert,
            window=TIGHT,
        ).prepare_async(_Payload(transcript), scope="ns/s1/r1")
    )
    assert first["context_state"]["request_fingerprint"] == second["context_state"]["request_fingerprint"]
    assert first["context_state"]["effective_state_fingerprint"] == second["context_state"]["effective_state_fingerprint"]
    assert first["context_state"]["source_head"] == second["context_state"]["source_head"]
    assert first["view"] == second["view"], "the same inputs produce the same request view"
