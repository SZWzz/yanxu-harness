"""Layer 4, part one: policy and approvals.

A tool call reaches the executor only through a decision. Two different things
produce that decision and they must not be confused:

* **Policy** is deployment configuration: "in this deployment, a write needs the
  author's approval; a read does not". It is decided server-side and depends
  only on the definition, the server-side context and whether the deployment
  exposed the tool at all.
* **Approval** is one author's decision about one concrete call. It is bound to
  the exact arguments, the target version, the preview, the tool contract, the
  policy version, the namespace and an expiry. Change any of them and the
  approval no longer applies.

A client submission (an HTTP body, a browser event) expresses a decision; it
never *is* the approval record. :class:`ApprovalLedger` is where the server
records decisions, and the ledger, not the caller, decides whether an approval
is valid right now.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from ..errors import ContractError
from ..versioning import fingerprint
from .definition import ToolDefinition

__all__ = [
    "Approval",
    "ApprovalBinding",
    "ApprovalStatus",
    "ApprovalStore",
    "AuthorizationPolicy",
    "ConservativePolicy",
    "POLICY_VERSION",
    "PolicyDecision",
    "PolicyKind",
    "check_binding",
    "binding_key",
]

#: Version of the policy semantics themselves. Part of every approval binding so
#: that a policy change invalidates approvals granted under the old policy.
POLICY_VERSION = "1"


class PolicyKind(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyDecision:
    kind: PolicyKind
    reason: str = ""
    policy_version: str = POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PolicyKind):
            raise ContractError(f"policy kind must be a PolicyKind, got {self.kind!r}")


@runtime_checkable
class AuthorizationPolicy(Protocol):
    """Deployment policy for one tool call."""

    def decide(self, *, definition: ToolDefinition, context: Any, exposed: bool) -> PolicyDecision:
        """Decide how ``definition`` may run for ``context``."""


@dataclass(frozen=True)
class ConservativePolicy:
    """The default policy: nothing that writes runs without an explicit decision.

    * a tool the deployment did not expose is denied,
    * ``read`` runs,
    * ``propose`` runs (it stages a suggestion; it cannot change canon),
    * ``write`` requires an approval unless the deployment explicitly opted into
      autonomous writes with ``auto_write=True``.
    """

    policy_version: str = POLICY_VERSION
    auto_write: bool = False

    def decide(self, *, definition: ToolDefinition, context: Any, exposed: bool) -> PolicyDecision:
        if not exposed:
            return PolicyDecision(
                PolicyKind.DENY, f"{definition.name} is not exposed by this deployment", self.policy_version
            )
        if definition.writes and not self.auto_write:
            return PolicyDecision(
                PolicyKind.REQUIRE_APPROVAL,
                f"{definition.name} changes authoritative state",
                self.policy_version,
            )
        return PolicyDecision(PolicyKind.ALLOW, "", self.policy_version)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalBinding:
    """Everything one approval is bound to.

    ``args_hash`` is a fingerprint of the *validated* argument mapping.
    ``target_version`` is the version of the object the call will change (or
    ``None`` when the call creates a new one). ``preview_hash`` binds the exact
    description the approver saw.
    """

    namespace: str
    call_id: str
    tool_name: str
    tool_version: str
    args_hash: str
    target_version: str | None = None
    preview_hash: str | None = None
    policy_version: str = POLICY_VERSION
    expires_at: float | None = None

    def __post_init__(self) -> None:
        for name in ("namespace", "call_id", "tool_name", "tool_version", "args_hash"):
            if not getattr(self, name):
                raise ContractError(f"approval binding requires a non-empty {name}")

    def key(self) -> str:
        """The stable identity of this approval (the store's primary key)."""
        return binding_key(self)


def binding_key(binding: ApprovalBinding) -> str:
    """The stable identity of one approval: which call, in which namespace.

    The *bound values* (argument hash, target version, tool version, policy
    version, preview) are deliberately **not** part of the key. They are what the
    approval is checked against, and keeping them out is what lets the gateway
    find the earlier decision and say *why* it no longer applies ("the arguments
    changed after approval") instead of silently treating a changed call as a
    brand-new one. The expiry is excluded for the same reason: extending a
    deadline must not create a second approval for the same call.
    """
    return fingerprint(
        {
            "namespace": binding.namespace,
            "call_id": binding.call_id,
            "tool_name": binding.tool_name,
        }
    )


def check_binding(
    binding: ApprovalBinding,
    *,
    args_hash: str,
    target_version: str | None,
    tool_version: str | None = None,
    preview_hash: str | None = None,
    policy_version: str = POLICY_VERSION,
    now: float | None = None,
) -> tuple[bool, str]:
    """Return ``(valid, reason)`` for a call happening now.

    Every drift is a separate reason so a host can explain *why* it asked again
    instead of just saying "no".
    """
    moment = time.time() if now is None else now
    if binding.expires_at is not None and moment > binding.expires_at:
        return False, "approval expired"
    if binding.args_hash != args_hash:
        return False, "arguments changed after approval"
    if binding.target_version != target_version:
        return False, "target version changed after approval"
    if tool_version is not None and binding.tool_version != tool_version:
        return False, "tool contract version changed after approval"
    if preview_hash is not None and binding.preview_hash != preview_hash:
        return False, "approval preview changed after approval"
    if binding.policy_version != policy_version:
        return False, "policy version changed after approval"
    return True, ""


@dataclass(frozen=True)
class Approval:
    """One server-recorded approval fact."""

    binding: ApprovalBinding
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str = ""
    decided_at: float | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ApprovalStatus):
            raise ContractError(f"approval status must be an ApprovalStatus, got {self.status!r}")

    @property
    def key(self) -> str:
        return self.binding.key()

    def is_approved(self) -> bool:
        return self.status is ApprovalStatus.APPROVED

    def is_open(self) -> bool:
        return self.status is ApprovalStatus.PENDING


@runtime_checkable
class ApprovalStore(Protocol):
    """Where approval facts live. Durable hosts back this with their own storage."""

    async def get(self, key: str) -> Approval | None:
        """Return the recorded approval for ``key``, if any."""

    async def put(self, approval: Approval) -> Approval:
        """Record ``approval`` as a brand-new fact for a call that has none."""

    async def supersede(self, binding: ApprovalBinding) -> Approval:
        """Ask again for the same call after its bound values moved.

        This is *not* a way to overwrite a decision: it records a new pending
        question whose binding differs from the recorded one, and the previous
        record must be kept as history. A store that instead keeps a single
        mutable row would lose the fact that the author already answered a
        different question.
        """

    async def decide(self, key: str, status: ApprovalStatus, decided_by: str, note: str = "") -> Approval:
        """Record a decision. Deciding twice with the same value is idempotent."""


def preview_hash(preview: Mapping[str, Any] | None) -> str | None:
    """Fingerprint an approval preview, or ``None`` when there is nothing to bind."""
    if preview is None:
        return None
    return fingerprint(dict(preview))


@dataclass
class InMemoryApprovalStore:
    """A volatile approval ledger for tests and single-process hosts.

    It records facts, not intentions: :meth:`decide` refuses to move a decided
    approval to a different terminal status, which is what makes concurrent
    approve/deny submissions idempotent instead of last-writer-wins.
    :meth:`supersede` keeps the superseded record in :attr:`history` rather than
    deleting it, so "the author approved a different version earlier" stays
    visible.
    """

    _approvals: dict[str, Approval] = field(default_factory=dict)
    history: list[Approval] = field(default_factory=list)

    async def get(self, key: str) -> Approval | None:
        return self._approvals.get(key)

    async def put(self, approval: Approval) -> Approval:
        self._approvals[approval.key] = approval
        return approval

    async def supersede(self, binding: ApprovalBinding) -> Approval:
        key = binding.key()
        current = self._approvals.get(key)
        if current is not None:
            if current.binding == binding:
                return current
            self.history.append(current)
        fresh = Approval(binding=binding, status=ApprovalStatus.PENDING)
        self._approvals[key] = fresh
        return fresh

    async def decide(
        self, key: str, status: ApprovalStatus, decided_by: str, note: str = ""
    ) -> Approval:
        current = self._approvals.get(key)
        if current is None:
            raise ContractError(f"{key}: no approval recorded for this call")
        if current.status is status:
            return current
        if not current.is_open():
            raise ContractError(
                f"{key}: approval already decided as {current.status.value}; refusing to overwrite it"
            )
        decided = Approval(
            binding=current.binding,
            status=status,
            decided_by=decided_by,
            decided_at=time.time(),
            note=note,
        )
        self._approvals[key] = decided
        return decided

    def open_approvals(self) -> tuple[Approval, ...]:
        return tuple(a for a in self._approvals.values() if a.is_open())
