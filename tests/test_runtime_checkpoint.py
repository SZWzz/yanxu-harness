"""Checkpoints: the partition that makes resume correct, and the drift that stops it.

The tests here are about the two ways a resume goes wrong: replaying a side effect
the checkpoint had already recorded, and continuing under an arrangement the
author's approval was not granted for. Both are refused rather than guessed.
"""

from __future__ import annotations

import pytest

from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.runtime import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointToolCall,
    PendingInteractionRef,
    RunCheckpoint,
    RunIdentity,
    RunStatus,
    plan_resume,
    resolve_resume,
)


def identity(**overrides: str) -> RunIdentity:
    values = {"namespace": "project-a", "session_id": "s1", "run_id": "r1", "attempt_id": "a1"}
    values.update(overrides)
    return RunIdentity(**values)  # type: ignore[arg-type]


def call(call_id: str, tool_name: str = "record.write") -> CheckpointToolCall:
    return CheckpointToolCall(call_id=call_id, tool_name=tool_name, arguments={"value": call_id})


def interaction(call_id: str, *, expires_at: float | None = None) -> PendingInteractionRef:
    return PendingInteractionRef(
        interaction_id=f"i-{call_id}",
        call_id=call_id,
        tool_name="record.write",
        args_hash=f"sha256:{call_id}",
        expires_at=expires_at,
    )


def suspended(**overrides) -> RunCheckpoint:
    """A run that suspended in the middle of a three-call batch: one done, two left."""
    values: dict = {
        "identity": identity(),
        "status": RunStatus.SUSPENDED,
        "messages": ({"role": "user", "content": "go"},),
        "pending_tool_calls": (call("c1"), call("c2"), call("c3")),
        "executed_tool_call_ids": frozenset({"c1"}),
        "unexecuted_tool_call_ids": frozenset({"c2", "c3"}),
        "pending_interactions": (interaction("c2"), interaction("c3")),
        "config_fingerprint": "sha256:config",
        "tools_fingerprint": "sha256:tools",
        "instructions_fingerprint": "sha256:instructions",
        "policy_fingerprint": "sha256:policy",
        "transcript_head": "sha256:head",
    }
    values.update(overrides)
    return RunCheckpoint(**values)


# --- the partition ------------------------------------------------------


def test_a_suspended_checkpoint_carries_the_batch_and_the_partition() -> None:
    checkpoint = suspended()
    assert [item.call_id for item in checkpoint.executed_calls()] == ["c1"]
    assert [item.call_id for item in checkpoint.unexecuted_calls()] == ["c2", "c3"]
    assert checkpoint.resumable


def test_the_partition_must_cover_the_declared_batch_exactly() -> None:
    """「未执行」与「没这批调用」是两件事；分不清就只能猜。"""
    with pytest.raises(ContractError, match="must cover exactly"):
        suspended(unexecuted_tool_call_ids=frozenset({"c2"}))  # c3 无归属
    with pytest.raises(ContractError, match="must cover exactly"):
        suspended(unexecuted_tool_call_ids=frozenset({"c2", "c3", "c9"}))  # c9 没声明


def test_a_call_cannot_be_on_both_sides() -> None:
    with pytest.raises(ContractError, match="both executed and unexecuted"):
        suspended(
            executed_tool_call_ids=frozenset({"c1", "c2"}),
            unexecuted_tool_call_ids=frozenset({"c2", "c3"}),
        )


def test_a_suspended_checkpoint_must_have_work_left_and_someone_to_answer() -> None:
    with pytest.raises(ContractError, match="interactions it is waiting for"):
        suspended(pending_interactions=())
    with pytest.raises(ContractError, match="at least"):
        suspended(unexecuted_tool_call_ids=frozenset(), executed_tool_call_ids=frozenset({"c1", "c2", "c3"}))


def test_a_terminal_checkpoint_is_not_resumable() -> None:
    finished = suspended(
        status=RunStatus.COMPLETED,
        pending_interactions=(),
        unexecuted_tool_call_ids=frozenset(),
        executed_tool_call_ids=frozenset({"c1", "c2", "c3"}),
    )
    assert not finished.resumable
    assert not resolve_resume(finished).ok


def test_marking_a_call_executed_moves_it_once_and_is_idempotent() -> None:
    checkpoint = suspended()
    after = checkpoint.with_execution("c2")
    assert "c2" in after.executed_tool_call_ids and "c2" not in after.unexecuted_tool_call_ids
    assert after.with_execution("c2") is after, "再次标记同一笔不得改动任何东西"
    with pytest.raises(ContractError, match="not part of this checkpoint"):
        after.with_execution("c9")
    # 原来的 checkpoint 不被就地改写。
    assert "c2" in checkpoint.unexecuted_tool_call_ids


# --- serialization ------------------------------------------------------


def test_a_checkpoint_round_trips_exactly() -> None:
    checkpoint = suspended()
    record = checkpoint.to_record()
    restored = RunCheckpoint.from_record(record)
    assert restored == checkpoint
    assert restored.to_record() == record


def test_the_record_is_json_shaped() -> None:
    import json

    record = suspended().to_record()
    assert json.loads(json.dumps(record, ensure_ascii=False)) == record


def test_a_future_schema_version_is_refused() -> None:
    record = dict(suspended().to_record())
    record["schema_version"] = CHECKPOINT_SCHEMA_VERSION + 1
    with pytest.raises(VersionRefused):
        RunCheckpoint.from_record(record)


def test_a_missing_schema_version_is_refused_with_a_reason() -> None:
    record = dict(suspended().to_record())
    del record["schema_version"]
    with pytest.raises(ContractError, match="cannot be migrated"):
        RunCheckpoint.from_record(record)


def test_an_unusable_identity_is_refused_not_partially_restored() -> None:
    record = dict(suspended().to_record())
    record["identity"] = {"namespace": "project-a"}
    with pytest.raises(ContractError, match="identity is unusable"):
        RunCheckpoint.from_record(record)


def test_a_corrupt_partition_in_a_record_is_refused_on_load() -> None:
    record = dict(suspended().to_record())
    record["unexecuted_tool_call_ids"] = ["c2"]
    with pytest.raises(ContractError, match="must cover exactly"):
        RunCheckpoint.from_record(record)


# --- drift --------------------------------------------------------------


def test_a_matching_arrangement_resumes() -> None:
    decision = resolve_resume(
        suspended(),
        config_fingerprint="sha256:config",
        tools_fingerprint="sha256:tools",
        instructions_fingerprint="sha256:instructions",
        policy_fingerprint="sha256:policy",
    )
    assert decision.ok and decision.drift == ()


@pytest.mark.parametrize("field", ["config", "tools", "instructions", "policy"])
def test_a_drifted_fingerprint_is_named(field: str) -> None:
    decision = resolve_resume(suspended(), **{f"{field}_fingerprint": "sha256:different"})
    assert not decision.ok
    assert decision.drift == (field,)
    assert field in decision.reason


def test_an_unchecked_fingerprint_is_not_a_drift() -> None:
    """没给当前值的维度不参与判定 —— 「没检查」不等于「不一致」。"""
    assert resolve_resume(suspended()).ok


def test_a_changed_exposure_manifest_is_drift() -> None:
    checkpoint = suspended(exposure_manifest={"selected_tool_names": ["a"]})
    assert resolve_resume(checkpoint, exposure_manifest={"selected_tool_names": ["a"]}).ok
    drifted = resolve_resume(checkpoint, exposure_manifest={"selected_tool_names": ["a", "b"]})
    assert not drifted.ok and drifted.drift == ("exposure",)


def test_an_expired_interaction_goes_back_to_the_author() -> None:
    checkpoint = suspended(pending_interactions=(interaction("c2", expires_at=100.0), interaction("c3")))
    decision = resolve_resume(checkpoint, now=200.0)
    assert not decision.ok
    assert decision.drift == ("expired",)
    assert "asked again" in decision.reason


# --- the plan -----------------------------------------------------------


def test_the_plan_never_replays_an_executed_call() -> None:
    plan = plan_resume(suspended())
    assert [item.call_id for item in plan.replay] == ["c2", "c3"]
    assert plan.already_done == ("c1",)
    assert "c1" not in [item.call_id for item in plan.replay]
    assert not plan.requires_author_revalidation


def test_drift_sends_the_plan_back_for_reconfirmation_with_the_drift_named() -> None:
    plan = plan_resume(suspended(), policy_fingerprint="sha256:other")
    assert plan.requires_author_revalidation
    assert "policy" in plan.reason
    # 漂移时**仍然**不把已执行的调用放进 replay。
    assert [item.call_id for item in plan.replay] == ["c2", "c3"]
    assert plan.already_done == ("c1",)


def test_the_plan_replays_only_the_calls_the_interactions_are_about() -> None:
    """挂起批次里如果有既没执行、也没有待答问题的调用，它需要先被重新确认。"""
    checkpoint = suspended(pending_interactions=(interaction("c2"),))
    plan = plan_resume(checkpoint)
    assert [item.call_id for item in plan.replay] == ["c2"]


def test_a_checkpoint_cannot_carry_a_host_engine_identity_by_accident() -> None:
    """中立类型不搬宿主的 `engine_id`：那是宿主序列化格式的一部分。"""
    fields = set(RunCheckpoint.__dataclass_fields__)
    assert "engine_id" not in fields
    assert "schema_version" in fields
    record = suspended().to_record()
    assert "engine_id" not in record
