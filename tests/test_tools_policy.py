"""Deployment policy, approval binding, and the invalidation matrix.

The invalidation matrix is the heart of this file: for each field an approval is
bound to, changing that field must make the approval stop applying. An approval
that survives an argument change is an approval for a different call.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.tools import (
    POLICY_VERSION,
    Approval,
    ApprovalBinding,
    ApprovalStatus,
    ConservativePolicy,
    InMemoryApprovalStore,
    PolicyKind,
    ToolDefinition,
    ToolEffect,
    binding_key,
    check_binding,
    preview_hash,
)
from yanxu_harness.tools.policy import preview_hash as preview_hash_direct


def validator(arguments: dict) -> dict:
    return {"path": arguments["path"]}


def handler(_context, arguments):
    return arguments


def read_tool() -> ToolDefinition:
    return ToolDefinition("notes.read", "Read a note", args_validator=validator, handler=handler)


def write_tool() -> ToolDefinition:
    return ToolDefinition(
        "notes.write",
        "Write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=validator,
        handler=handler,
        preview=lambda _c, a: {"path": a["path"], "action": "replace"},
    )


def binding(**overrides) -> ApprovalBinding:
    values = {
        "namespace": "project-a",
        "call_id": "call-1",
        "tool_name": "notes.write",
        "tool_version": "1",
        "args_hash": "sha256:aaa",
        "target_version": "v3",
        "preview_hash": "sha256:ppp",
        "policy_version": POLICY_VERSION,
        "expires_at": None,
    }
    values.update(overrides)
    return ApprovalBinding(**values)  # type: ignore[arg-type]


# -- policy -------------------------------------------------------------


def test_a_hidden_tool_is_denied_before_anything_else() -> None:
    decision = ConservativePolicy().decide(definition=write_tool(), context=None, exposed=False)
    assert decision.kind is PolicyKind.DENY
    assert "not exposed" in decision.reason


def test_reads_run_and_writes_need_an_approval_by_default() -> None:
    policy = ConservativePolicy()
    assert policy.decide(definition=read_tool(), context=None, exposed=True).kind is PolicyKind.ALLOW
    assert (
        policy.decide(definition=write_tool(), context=None, exposed=True).kind
        is PolicyKind.REQUIRE_APPROVAL
    )
    assert (
        ConservativePolicy(auto_write=True)
        .decide(definition=write_tool(), context=None, exposed=True)
        .kind
        is PolicyKind.ALLOW
    )


def test_a_policy_decision_must_use_the_vocabulary() -> None:
    with pytest.raises(ContractError):
        from yanxu_harness.tools import PolicyDecision

        PolicyDecision(kind="allow")  # type: ignore[arg-type]


# -- approvals ----------------------------------------------------------


def test_an_approval_binding_requires_every_identity_field() -> None:
    for field in ("namespace", "call_id", "tool_name", "tool_version", "args_hash"):
        with pytest.raises(ContractError, match=field):
            binding(**{field: ""})


def test_the_binding_key_ignores_the_expiry() -> None:
    assert binding_key(binding()) == binding_key(binding(expires_at=time.time() + 60))


def test_one_call_has_one_approval_key_whatever_the_bound_values_do() -> None:
    """The key is the call; the bound values are what the approval is checked *against*.

    If the bound values were part of the key, a changed argument set would look
    like a brand-new call and the gateway could not report "the arguments changed
    after approval" — it would just ask again with no explanation, and the earlier
    decision would be invisible.
    """
    base = binding()
    assert binding_key(base) == binding_key(binding(args_hash="sha256:other"))
    assert binding_key(base) == binding_key(binding(target_version="v9"))
    assert binding_key(base) == binding_key(binding(tool_version="2"))
    assert binding_key(base) == binding_key(binding(policy_version="2"))
    # A different call, a different namespace or a different tool is a different key.
    assert binding_key(base) != binding_key(binding(call_id="call-2"))
    assert binding_key(base) != binding_key(binding(namespace="project-b"))
    assert binding_key(base) != binding_key(binding(tool_name="notes.other"))


def test_a_tool_contract_change_invalidates_an_approval() -> None:
    approved = binding(tool_version="1")
    valid, message = check_binding(
        approved,
        args_hash=approved.args_hash,
        target_version=approved.target_version,
        tool_version="2",
    )
    assert not valid
    assert "tool contract version changed" in message


def test_a_preview_change_invalidates_an_approval() -> None:
    approved = binding(preview_hash="sha256:ppp")
    valid, message = check_binding(
        approved,
        args_hash=approved.args_hash,
        target_version=approved.target_version,
        preview_hash="sha256:qqq",
    )
    assert not valid
    assert "preview changed" in message


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"args_hash": "sha256:bbb"}, "arguments changed"),
        ({"target_version": "v4"}, "target version changed"),
        ({"policy_version": "2"}, "policy version changed"),
    ],
)
def test_drift_invalidates_an_approval(overrides: dict, reason: str) -> None:
    approved = binding()
    valid, message = check_binding(
        approved,
        args_hash=overrides.get("args_hash", approved.args_hash),
        target_version=overrides.get("target_version", approved.target_version),
        policy_version=overrides.get("policy_version", approved.policy_version),
    )
    assert not valid
    assert reason in message


def test_an_expired_approval_does_not_apply() -> None:
    approved = binding(expires_at=time.time() - 1)
    valid, message = check_binding(
        approved, args_hash=approved.args_hash, target_version=approved.target_version
    )
    assert not valid
    assert message == "approval expired"


def test_a_matching_approval_is_accepted() -> None:
    approved = binding()
    valid, message = check_binding(
        approved, args_hash=approved.args_hash, target_version=approved.target_version
    )
    assert valid and message == ""


def test_a_preview_hash_is_only_produced_when_there_is_a_preview() -> None:
    assert preview_hash(None) is None
    assert preview_hash({"a": 1}) == preview_hash_direct({"a": 1})
    assert preview_hash({"a": 1}) != preview_hash({"a": 2})


# -- the store ----------------------------------------------------------


def test_a_decision_is_recorded_once_and_cannot_be_overwritten() -> None:
    async def scenario() -> None:
        store = InMemoryApprovalStore()
        approved = Approval(binding=binding(), status=ApprovalStatus.PENDING)
        await store.put(approved)
        first = await store.decide(approved.key, ApprovalStatus.APPROVED, "author")
        assert first.status is ApprovalStatus.APPROVED
        # Repeating the same decision is idempotent, not a second write.
        again = await store.decide(approved.key, ApprovalStatus.APPROVED, "author")
        assert again.decided_at == first.decided_at
        # A conflicting decision is refused rather than silently overwriting.
        with pytest.raises(ContractError, match="refusing to overwrite"):
            await store.decide(approved.key, ApprovalStatus.DENIED, "author")
        assert (await store.get(approved.key)).status is ApprovalStatus.APPROVED
        assert store.open_approvals() == ()

    asyncio.run(scenario())


def test_deciding_an_unknown_call_is_refused() -> None:
    async def scenario() -> None:
        store = InMemoryApprovalStore()
        with pytest.raises(ContractError, match="no approval recorded"):
            await store.decide("sha256:missing", ApprovalStatus.APPROVED, "author")

    asyncio.run(scenario())


def test_a_pending_approval_stays_open_until_decided() -> None:
    async def scenario() -> None:
        store = InMemoryApprovalStore()
        pending = Approval(binding=binding())
        await store.put(pending)
        assert store.open_approvals() == (pending,)
        assert not pending.is_approved()

    asyncio.run(scenario())
