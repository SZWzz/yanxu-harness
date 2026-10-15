"""Run identity, server-side authority, terminal states and turn budgets."""

from __future__ import annotations

import pytest

from yanxu_harness.errors import ConfigurationError, ContractError
from yanxu_harness.runtime import (
    Cancellation,
    Principal,
    RunContext,
    RunIdentity,
    RunOutcome,
    RunRequest,
    RunStatus,
    is_terminal_status,
    validate_max_turns,
)


def identity(**overrides: str) -> RunIdentity:
    values = {
        "namespace": "project-a",
        "session_id": "s1",
        "run_id": "r1",
        "attempt_id": "a1",
    }
    values.update(overrides)
    return RunIdentity(**values)  # type: ignore[arg-type]


def context() -> RunContext:
    return RunContext(
        identity=identity(),
        principal=Principal(subject="author", permissions=frozenset({"story.write"})),
    )


@pytest.mark.parametrize("value", [0, -1, True, False, "3", 2.0])
def test_an_invalid_turn_budget_is_refused(value: object) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        validate_max_turns(value)
    assert "invalid_max_turns" in str(excinfo.value)


def test_none_means_unbounded_and_positive_ints_pass() -> None:
    assert validate_max_turns(None) is None
    assert validate_max_turns(1) == 1
    assert validate_max_turns(7) == 7


def test_a_request_cannot_smuggle_an_invalid_budget() -> None:
    with pytest.raises(ConfigurationError):
        RunRequest(prompt="hi", max_turns=0)


def test_authority_cannot_be_established_by_a_caller() -> None:
    with pytest.raises(ContractError):
        Principal(subject="model", permissions=frozenset({"story.write"}), established_by="tool-args")


def test_a_call_context_inherits_authority_and_cancellation() -> None:
    parent = context()
    child = parent.for_tool_call("call-1")
    assert child.principal is parent.principal
    assert child.cancellation is parent.cancellation
    assert child.services is parent.services
    assert child.tool_call_id == "call-1"
    assert child.identity == parent.identity


def test_identity_requires_every_core_identifier() -> None:
    for field in ("namespace", "session_id", "run_id", "attempt_id"):
        with pytest.raises(ContractError, match=field):
            identity(**{field: ""})


def test_operation_identity_stamps_one_operation() -> None:
    stamped = identity().for_operation("op-9")
    assert stamped.operation_id == "op-9"
    assert stamped.run_id == "r1"
    with pytest.raises(ContractError):
        identity().for_operation("")


def test_cancellation_is_written_once() -> None:
    token = Cancellation()
    assert not token.cancelled
    token.cancel("author stopped the run")
    token.cancel("second reason is ignored")
    assert token.cancelled
    assert token.reason == "author stopped the run"


def test_terminal_statuses_exclude_suspension() -> None:
    assert is_terminal_status(RunStatus.COMPLETED)
    assert is_terminal_status("usage_limited")
    assert not is_terminal_status(RunStatus.SUSPENDED)


def test_a_suspended_outcome_must_carry_its_interaction() -> None:
    with pytest.raises(ContractError):
        RunOutcome(status=RunStatus.SUSPENDED, run_id="r1")


def test_a_usage_limited_outcome_must_explain_itself() -> None:
    with pytest.raises(ContractError):
        RunOutcome(status=RunStatus.USAGE_LIMITED, run_id="r1")
    outcome = RunOutcome(
        status=RunStatus.USAGE_LIMITED, run_id="r1", error_code="usage_limit_exceeded", turns_used=1
    )
    assert outcome.turns_used == 1
