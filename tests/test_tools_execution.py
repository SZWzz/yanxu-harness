"""Explicit effect states: what can be proven, and what never gets retried."""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.errors import ContractError, OutcomeUnknown
from yanxu_harness.tools.outcomes import (
    ToolArgsInvalid,
    ToolCancelled,
    ToolDeferred,
    ToolRejected,
)
from yanxu_harness.tools import (
    EffectState,
    ToolDefinition,
    ToolEffect,
    ToolIntent,
    ToolReceipt,
    classify_failure,
    is_settled,
    needs_reconciliation,
    should_retry,
)
from yanxu_harness.testing import RecordingExecutionBackend, failing_handler


def validator(arguments: dict) -> dict:
    return {"path": arguments["path"]}


def _intent(operation_id: str = "op-1") -> ToolIntent:
    return ToolIntent(
        operation_id=operation_id,
        namespace="project-a",
        tool_name="notes.write",
        tool_version="1",
        args_hash="sha256:aaa",
    )


def _definition(handler) -> ToolDefinition:
    return ToolDefinition(
        "notes.write",
        "Write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=validator,
        handler=handler,
        preview=lambda _c, a: {"path": a["path"]},
    )


def test_an_intent_must_name_its_operation_and_scope() -> None:
    for field in ("operation_id", "namespace", "tool_name", "tool_version", "args_hash"):
        values = {
            "operation_id": "op-1",
            "namespace": "ns",
            "tool_name": "t",
            "tool_version": "1",
            "args_hash": "sha256:x",
        }
        values[field] = ""
        with pytest.raises(ContractError, match=field):
            ToolIntent(**values)  # type: ignore[arg-type]


def test_a_succeeded_receipt_must_carry_a_result() -> None:
    with pytest.raises(ContractError, match="must carry a result"):
        ToolReceipt(operation_id="op-1", state=EffectState.SUCCEEDED)
    assert ToolReceipt(operation_id="op-1", state=EffectState.SUCCEEDED, result={}).result == {}


def test_unknown_receipts_are_explicit_and_need_reconciliation() -> None:
    receipt = ToolReceipt(operation_id="op-1", state=EffectState.UNKNOWN, detail="write may have landed")
    assert receipt.needs_reconciliation
    assert needs_reconciliation(EffectState.UNKNOWN)
    assert not needs_reconciliation(EffectState.FAILED)
    with pytest.raises(OutcomeUnknown) as excinfo:
        receipt.raise_for_state()
    assert "not be retried automatically" in str(excinfo.value)


def test_settled_states_are_the_terminal_ones() -> None:
    assert is_settled(EffectState.SUCCEEDED)
    assert is_settled(EffectState.FAILED)
    assert is_settled(EffectState.INVALID_ARGS)
    assert is_settled(EffectState.UNKNOWN)
    assert not is_settled(EffectState.QUEUED)
    assert not is_settled(EffectState.RUNNING)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ToolArgsInvalid("bad args"), EffectState.INVALID_ARGS),
        (ToolRejected("notes.write: no"), EffectState.REJECTED),
        (ToolDeferred("waiting for the author"), EffectState.DEFERRED),
        (ToolCancelled("stopped"), EffectState.CANCELLED),
        (TimeoutError("timed out"), EffectState.UNKNOWN),
        (ConnectionError("reset"), EffectState.UNKNOWN),
        (OSError("socket"), EffectState.UNKNOWN),
        (RuntimeError("handler exploded after the request"), EffectState.UNKNOWN),
    ],
)
def test_failures_are_classified_conservatively(exc: BaseException, expected: EffectState) -> None:
    state, detail = classify_failure(exc)
    assert state is expected
    assert detail


def test_invalid_arguments_are_their_own_state_not_a_failure() -> None:
    """`ToolArgsInvalid` 的语义是「改好了再来」，不是「这次成了」也不是「工具坏了」。

    把它折成 FAILED 会丢掉模型接下来唯一该知道的事；宿主自己的词汇已经把两者分开
    （`agent_tool_outcomes.ToolArgsInvalid` vs `ToolRejected`），harness 不该再抹平。
    """
    state, detail = classify_failure(ToolArgsInvalid("value 不是字符串"))
    assert state is EffectState.INVALID_ARGS
    assert detail == "value 不是字符串"
    assert not should_retry(state), "参数错了不该由 harness 自己重试 —— 改参数是调用方的事"
    receipt = ToolReceipt(operation_id="op-1", state=EffectState.INVALID_ARGS, detail="改参数")
    with pytest.raises(ToolArgsInvalid):
        receipt.raise_for_state()


def test_an_unproven_outcome_is_never_retried() -> None:
    assert not should_retry(EffectState.UNKNOWN)
    assert not should_retry(EffectState.UNKNOWN, idempotent=True)
    assert should_retry(EffectState.FAILED, idempotent=True)
    assert not should_retry(EffectState.FAILED)
    assert not should_retry(EffectState.SUCCEEDED)
    assert not should_retry(EffectState.RUNNING)
    assert not should_retry(EffectState.REJECTED)
    assert not should_retry(EffectState.CANCELLED)
    assert should_retry(EffectState.QUEUED)
    assert not should_retry(EffectState.INVALID_ARGS)


def test_a_handler_runs_exactly_once_and_its_receipt_is_recorded() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        def handler(_context, arguments):
            calls.append(arguments["path"])
            return {"written": arguments["path"]}

        backend = RecordingExecutionBackend()
        intent = _intent()
        receipt = await backend.execute(intent, _definition(handler), {"path": "a.md"}, None)
        assert receipt.state is EffectState.SUCCEEDED
        assert backend.result_for("op-1") == {"written": "a.md"}
        assert calls == ["a.md"]
        await backend.settle(receipt)
        assert backend.settled == [receipt]

    asyncio.run(scenario())


def test_a_handler_that_times_out_produces_an_unknown_receipt() -> None:
    async def scenario() -> None:
        backend = RecordingExecutionBackend()
        definition = _definition(failing_handler(TimeoutError("no receipt")))
        receipt = await backend.execute(_intent("op-2"), definition, {"path": "a.md"}, None)
        assert receipt.state is EffectState.UNKNOWN
        assert not should_retry(receipt.state)

    asyncio.run(scenario())


def test_begin_replays_an_already_recorded_receipt() -> None:
    async def scenario() -> None:
        backend = RecordingExecutionBackend()
        intent = _intent("op-3")
        assert await backend.begin(intent) is None
        receipt = await backend.execute(intent, _definition(failing_handler(RuntimeError("x"))), {"path": "a"}, None)
        assert await backend.begin(intent) is receipt
        assert len(backend.intents) == 2

    asyncio.run(scenario())


def test_a_backend_must_declare_its_ownership_and_durability() -> None:
    backend = RecordingExecutionBackend()
    assert backend.ownership == "harness"
    assert backend.durable is False
