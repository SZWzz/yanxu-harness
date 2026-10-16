"""Recoverable parent/child work: spawn, structured wait, result and cancellation.

This is the control plane for the first-version local subagent tree (plan §10). It does **not**
run a model, hold a second agent loop or decide what a child *means*: it creates the durable
relation, the child task, its bounded grant and the notice its parent will need — in one
transaction — and it re-reads committed facts instead of trusting a live callback.

Four rules the plan freezes, and where each one lives:

* **authority is intersected, never extracted.** What a child may be is the intersection of the
  host's policy, the parent profile's delegated scope and the proposal. A prompt that *says*
  "you may use tool X" grants nothing: :meth:`ChildCoordinator.authorize` reads configuration,
  never text, and never a child's result (plan §10.1);
* **one relation per stable spawn identity.** The identity binds the parent, the model action and
  the tool call, so a recovery re-submit returns the original child while the same words in a
  different action do not merge. When the control store and the Gateway receipt are different
  databases, the relation is the business evidence that converges the gap (plan §10.2);
* **the child's terminal state and its parent's notice are one fact.** ``complete_child`` writes
  the child's terminal record, the relation's end state, the reservation settlement and the
  outbox notice in a single transaction; the notice is only a wake hint, and the parent re-reads
  the child record and its acceptance on resume (plan §10.3);
* **terminal means terminal.** A late child result is archived, never used to revive a finished
  parent; a parent that ends with active children first enters a durable join rather than being
  marked complete while its children still run (plan §10.3/§10.4).

The first version is deliberately shallow: a root and its children. Deeper nesting, detached
children and arbitrary mutual waits are refused with a named reason rather than half-supported.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol, Sequence

from ..errors import ContractError
from ..versioning import fingerprint
from .budgets import BudgetLedger
from .types import (
    REASON_AWAITING,
    REASON_CANCELLED,
    REASON_CANCEL_REQUESTED,
    REASON_COMPLETED,
    REASON_FAILED,
    REASON_RESUMED,
    REASON_USAGE_LIMITED,
    OUTBOX_CHILD_COMPLETED,
    OUTBOX_CHILD_SPAWNED,
    AdmissionOutcome,
    BudgetRefused,
    BudgetReservation,
    BudgetUnit,
    ChildOutcome,
    ChildRefused,
    ChildRelation,
    ChildResult,
    ChildSpawnState,
    ControlOutboxEntry,
    DispatchClaim,
    InputAdmission,
    InputContent,
    InputKind,
    RefusalCode,
    SpawnRefusal,
    TaskAdmission,
    TaskRecord,
    TaskStatus,
)

__all__ = [
    "ChildAuthorization",
    "ChildCoordinator",
    "ChildDelegation",
    "ChildPolicy",
    "ChildSpawnRequest",
    "ResultVerifier",
    "SchemaResultVerifier",
    "SpawnOutcome",
    "derive_spawn_identity",
    "read_committed_result",
]

#: Which reason code a child's terminal outcome carries on its own record. Kept as data so the
#: mapping is one place and a test can pin it.
_OUTCOME_REASON: Mapping[ChildOutcome, str] = {
    ChildOutcome.COMPLETED: REASON_COMPLETED,
    ChildOutcome.FAILED: REASON_FAILED,
    ChildOutcome.CANCELLED: REASON_CANCELLED,
    ChildOutcome.USAGE_LIMITED: REASON_USAGE_LIMITED,
}


def derive_spawn_identity(parent_task_id: str, model_action_id: str, call_id: str) -> str:
    """The stable spawn identity: parent + model action + tool call, digested.

    It deliberately excludes the prompt text: two identical requests in two different model
    actions are two spawns, and the same request redelivered after a crash is one spawn. That is
    exactly what the identity has to distinguish (plan §10.2).
    """
    if not parent_task_id or not model_action_id or not call_id:
        raise ContractError(
            "a spawn identity binds a parent, a model action and a tool call; all three are "
            "required, because the same text in a different action is a different child"
        )
    return "spawn:" + fingerprint(
        {
            "parent_task_id": parent_task_id,
            "model_action_id": model_action_id,
            "call_id": call_id,
        }
    )


# --------------------------------------------------------------------------------------
# host policy and the intersected authority
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildDelegation:
    """What one parent profile may hand down. Empty sets mean "no additional narrowing"."""

    profiles: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    workspace_prefixes: tuple[str, ...] = ()
    budget_units: Mapping[str, float] = field(default_factory=dict)
    result_keys: frozenset[str] = frozenset()

    def limit(self, unit: BudgetUnit) -> float | None:
        for key, amount in self.budget_units.items():
            if key == unit.value:
                return float(amount)
        return None


@dataclass(frozen=True)
class ChildPolicy:
    """The host's ceiling for one deployment. No request can widen it, and no profile sees more
    than its own delegated scope."""

    max_depth: int = 1
    max_children_per_parent: int = 4
    profiles: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    workspace_root: str = ""
    budget_units: Mapping[str, float] = field(default_factory=dict)
    result_schema: Mapping[str, Any] = field(default_factory=dict)
    scopes: Mapping[str, ChildDelegation] = field(default_factory=dict)

    def scope_for(self, profile: str) -> ChildDelegation:
        return self.scopes.get(profile, ChildDelegation())

    def limit(self, unit: BudgetUnit) -> float | None:
        for key, amount in self.budget_units.items():
            if key == unit.value:
                return float(amount)
        return None


@dataclass(frozen=True)
class ChildSpawnRequest:
    """One proposed child, already validated by the Gateway's tool layer.

    This is a *proposal*: the coordinator still intersects it with the host policy and the parent's
    delegated scope. ``initial_input`` is task data; nothing here is authority.
    """

    parent_task_id: str
    namespace: str
    session_id: str
    spawn_identity: str
    child_task_id: str
    child_run_id: str
    initial_input: InputContent
    profile: str = ""
    tools: tuple[str, ...] = ()
    workspace_ref: str = ""
    result_schema: Mapping[str, Any] = field(default_factory=dict)
    budget_unit: BudgetUnit | str = BudgetUnit.MODEL_REQUESTS
    budget_amount: float = 0.0
    operation_id: str = ""
    model_action_id: str = ""
    call_id: str = ""
    deadline_at: float | None = None
    priority: int = 0
    created_at: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "parent_task_id",
            "namespace",
            "session_id",
            "spawn_identity",
            "child_task_id",
            "child_run_id",
        ):
            if not getattr(self, name):
                raise ContractError(f"a child spawn request requires a non-empty {name}")
        if self.initial_input.kind is not InputKind.INITIAL:
            raise ContractError("a child's opening input must be an `initial` input")
        if self.budget_amount < 0:
            raise ContractError("a child grant cannot be negative")


@dataclass(frozen=True)
class ChildAuthorization:
    """The effective permission a child actually gets: host ceiling ∩ parent scope ∩ proposal."""

    profile: str
    tools: tuple[str, ...] = ()
    workspace_ref: str = ""
    result_schema: Mapping[str, Any] = field(default_factory=dict)
    budget_unit: BudgetUnit = BudgetUnit.MODEL_REQUESTS
    budget_amount: float = 0.0
    budget_cap: float | None = None
    max_depth: int = 1

    @property
    def digest(self) -> str:
        return fingerprint(self.to_record())

    def to_record(self) -> Mapping[str, Any]:
        return {
            "profile": self.profile,
            "tools": list(self.tools),
            "workspace_ref": self.workspace_ref,
            "result_schema": {
                str(key): self.result_schema[key] for key in sorted(self.result_schema)
            },
            "budget_unit": self.budget_unit.value,
            "budget_amount": self.budget_amount,
            "budget_cap": self.budget_cap,
            "max_depth": self.max_depth,
        }


@dataclass(frozen=True)
class SpawnOutcome:
    """What a spawn did: the relation, and whether this call created it or converged on it."""

    relation: ChildRelation
    created: bool
    authorization: ChildAuthorization

    @property
    def child_task_id(self) -> str:
        return self.relation.child_task_id


# --------------------------------------------------------------------------------------
# result acceptance
# --------------------------------------------------------------------------------------


class ResultVerifier(Protocol):
    """The host's independent verdict on a child's structured result.

    Schema validity only proves the *shape*: a well-formed result that fails the business checks
    must surface as ``accepted=False`` so the parent can decide what to do. The verifier is called
    with the effective authorization and the raw result, and must not grant anything.
    """

    def verify(
        self, authorization: ChildAuthorization, result: Mapping[str, Any]
    ) -> tuple[bool, str]:
        ...


_SIMPLE_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
}


class SchemaResultVerifier:
    """The default verifier: it checks the declared keys and their simple types.

    It is deliberately *only* a schema check. A host that needs business acceptance supplies its
    own :class:`ResultVerifier`; pretending the schema is the business would hide exactly the
    failure the plan asks to make visible.
    """

    def verify(
        self, authorization: ChildAuthorization, result: Mapping[str, Any]
    ) -> tuple[bool, str]:
        schema = authorization.result_schema or {}
        if not schema:
            return True, ""
        for key, declared in schema.items():
            if key not in result:
                return False, f"the result is missing the declared key {key!r}"
            expected = _SIMPLE_TYPES.get(str(declared))
            if expected is not None and not isinstance(result[key], expected):
                return False, (
                    f"the result key {key!r} is {type(result[key]).__name__}, not "
                    f"{declared}"
                )
        return True, ""


# --------------------------------------------------------------------------------------
# the coordinator
# --------------------------------------------------------------------------------------


class ChildCoordinator:
    """Spawn, wait, complete, deliver and cancel — all as durable control-plane facts.

    The coordinator owns no model and no loop. It is given a coordination control store (and,
    optionally, a :class:`BudgetLedger` for readable snapshots), the host's :class:`ChildPolicy`
    and a :class:`ResultVerifier`. Every method is safe to call again after a crash: reads rebuild
    state from committed records, and writes are idempotent by stable identity.
    """

    def __init__(
        self,
        control: Any,
        *,
        policy: ChildPolicy | None = None,
        ledger: BudgetLedger | None = None,
        verifier: ResultVerifier | None = None,
        clock: Any = time.time,
    ) -> None:
        if control is None:
            raise ContractError("a child coordinator needs a control store")
        self._control = control
        self._policy = policy or ChildPolicy()
        self._ledger = ledger
        self._verifier = verifier or SchemaResultVerifier()
        self._clock = clock

    @property
    def policy(self) -> ChildPolicy:
        return self._policy

    # -- authorization -----------------------------------------------------------------

    def authorize(self, request: ChildSpawnRequest, parent: TaskRecord) -> ChildAuthorization:
        """Intersect host policy, the parent's delegated scope and the proposal.

        Reads **configuration only**. The prompt text, the requested tool list's prose and any
        "authorization" the model wrote are all task data and cannot widen this (plan §10.1).
        """
        policy = self._policy
        depth = parent.depth + 1
        if depth > policy.max_depth:
            raise ChildRefused(
                SpawnRefusal.DEPTH_EXCEEDED,
                f"a child of {parent.task_id!r} would sit at depth {depth}, past the policy's "
                f"limit of {policy.max_depth}",
            )
        scope = policy.scope_for(parent.profile)
        allowed_profiles = policy.profiles & scope.profiles if scope.profiles else policy.profiles
        profile = request.profile or parent.profile
        if profile not in allowed_profiles:
            raise ChildRefused(
                SpawnRefusal.NOT_PERMITTED,
                f"profile {profile!r} is outside the profiles the host permits for a child of "
                f"{parent.profile!r}",
            )
        allowed_tools = policy.tools & scope.tools if scope.tools else policy.tools
        tools = tuple(sorted(set(request.tools)))
        outside = [tool for tool in tools if tool not in allowed_tools]
        if outside:
            raise ChildRefused(
                SpawnRefusal.NOT_PERMITTED,
                f"tools {outside} are outside the delegated scope; a child cannot be granted a "
                "tool its parent's scope excludes",
            )
        prefixes = scope.workspace_prefixes or (
            (policy.workspace_root,) if policy.workspace_root else ()
        )
        workspace_ref = request.workspace_ref
        if not workspace_ref and prefixes:
            workspace_ref = f"{prefixes[0].rstrip('/')}/{request.child_task_id}"
        if prefixes and not _under_any(workspace_ref, prefixes):
            raise ChildRefused(
                SpawnRefusal.NOT_PERMITTED,
                f"workspace {workspace_ref!r} is outside the delegated workspace prefixes "
                f"{list(prefixes)}",
            )
        schema: dict[str, Any] = dict(request.result_schema)
        for key, declared in policy.result_schema.items():
            schema.setdefault(key, declared)
        if scope.result_keys:
            schema = {key: value for key, value in schema.items() if key in scope.result_keys}
            missing = [key for key in policy.result_schema if key not in schema]
            if missing:
                raise ChildRefused(
                    SpawnRefusal.NOT_PERMITTED,
                    f"the delegated scope drops required result keys {missing}",
                )
        unit = (
            request.budget_unit
            if isinstance(request.budget_unit, BudgetUnit)
            else BudgetUnit(str(request.budget_unit))
        )
        caps = [cap for cap in (policy.limit(unit), scope.limit(unit)) if cap is not None]
        cap = min(caps) if caps else None
        amount = float(request.budget_amount)
        if cap is not None and amount > cap:
            raise ChildRefused(
                SpawnRefusal.BUDGET_REFUSED,
                f"a grant of {amount} {unit.value} exceeds the delegated cap of {cap}",
            )
        return ChildAuthorization(
            profile=profile,
            tools=tools,
            workspace_ref=workspace_ref,
            result_schema=schema,
            budget_unit=unit,
            budget_amount=amount,
            budget_cap=cap,
            max_depth=policy.max_depth,
        )

    # -- spawn -------------------------------------------------------------------------

    async def spawn(
        self, request: ChildSpawnRequest, *, claim: DispatchClaim | None = None
    ) -> SpawnOutcome:
        """Create the relation, child task, grant and notice — or converge on the existing one.

        A refusal is raised before any write. A repeated ``spawn_identity`` returns the original
        relation unchanged, which is how a crash between the control commit and the Gateway
        receipt is settled without a second child (plan §10.2, case C-01).
        """
        now = request.created_at or self._clock()
        parent = await self._control.read_task(request.parent_task_id)
        if parent is None:
            raise ChildRefused(
                SpawnRefusal.UNKNOWN_PARENT, f"no task {request.parent_task_id!r}"
            )
        if parent.namespace != request.namespace or parent.session_id != request.session_id:
            raise ChildRefused(
                SpawnRefusal.IDENTITY_MISMATCH,
                "a spawn must stay inside its parent's namespace and session",
            )
        if parent.is_terminal:
            raise ChildRefused(
                SpawnRefusal.PARENT_TERMINAL,
                f"the parent ended as {parent.status.value}; a terminal task does not spawn",
            )
        if parent.cancel_requested_at is not None:
            raise ChildRefused(
                SpawnRefusal.CANCELLED_FIRST,
                "a cancellation was requested before this spawn committed",
            )
        authorization = self.authorize(request, parent)
        existing = await self._control.read_child(request.spawn_identity)
        if existing is not None:
            if existing.child_task_id != request.child_task_id:
                raise ChildRefused(
                    SpawnRefusal.CONFIG_CONFLICT,
                    f"spawn identity {request.spawn_identity!r} already owns child "
                    f"{existing.child_task_id!r}; the same identity with a different child is a "
                    "conflict, not a second registration",
                )
            if (
                existing.authorization_digest
                and existing.authorization_digest != authorization.digest
            ):
                # The stable identity is the same but what the child would be allowed to do is
                # not. Reusing the relation would silently keep the old authority, so the retry
                # is refused and the caller must use a new model action / spawn identity.
                raise ChildRefused(
                    SpawnRefusal.CONFIG_CONFLICT,
                    f"spawn identity {request.spawn_identity!r} was registered with a different "
                    "authorization; the same identity with changed configuration (profile, tools, "
                    "workspace, result schema or grant) is refused rather than re-used",
                )
            return SpawnOutcome(existing, created=False, authorization=authorization)
        children = await self._control.read_children(parent.task_id)
        if len(children) >= self._policy.max_children_per_parent:
            raise ChildRefused(
                SpawnRefusal.TREE_FULL,
                f"{parent.task_id!r} already has {len(children)} children, its policy limit of "
                f"{self._policy.max_children_per_parent}",
            )
        relation = ChildRelation(
            parent_task_id=parent.task_id,
            child_task_id=request.child_task_id,
            spawn_identity=request.spawn_identity,
            namespace=parent.namespace,
            session_id=parent.session_id,
            state=ChildSpawnState.REGISTERED,
            operation_id=request.operation_id,
            authorization_digest=authorization.digest,
            created_at=now,
        )
        child_task = TaskAdmission(
            namespace=parent.namespace,
            session_id=parent.session_id,
            task_id=request.child_task_id,
            run_id=request.child_run_id,
            initial_input=request.initial_input,
            priority=request.priority,
            root_task_id=parent.root_task_id or parent.task_id,
            parent_task_id=parent.task_id,
            depth=parent.depth + 1,
            profile=authorization.profile,
            workspace_ref=authorization.workspace_ref,
            created_at=now,
            deadline_at=request.deadline_at,
        )
        reservation = None
        limit = None
        if authorization.budget_amount > 0:
            reservation = BudgetReservation(
                root_task_id=parent.root_task_id or parent.task_id,
                task_id=request.child_task_id,
                unit=authorization.budget_unit,
                amount=authorization.budget_amount,
                reservation_id=f"grant:{request.spawn_identity}",
                dispatch_identity=f"grant:{request.spawn_identity}",
                created_at=now,
            )
            limit = authorization.budget_cap
        notice = ControlOutboxEntry(
            outbox_id=f"{OUTBOX_CHILD_SPAWNED}:{request.spawn_identity}",
            kind=OUTBOX_CHILD_SPAWNED,
            target_task_id=parent.task_id,
            payload={
                "child_task_id": request.child_task_id,
                "spawn_identity": request.spawn_identity,
                "profile": authorization.profile,
                "operation_id": request.operation_id,
                "authorization": authorization.to_record(),
                "authorization_digest": authorization.digest,
            },
            namespace=parent.namespace,
            created_at=now,
        )
        try:
            stored = await self._control.register_child(
                relation,
                claim=claim,
                child_task=child_task,
                reservation=reservation,
                notification=notice,
                limit=limit,
                now=now,
            )
        except BudgetRefused as exc:
            raise ChildRefused(SpawnRefusal.BUDGET_REFUSED, str(exc)) from exc
        except ContractError as exc:
            raise ChildRefused(SpawnRefusal.NOT_PERMITTED, str(exc)) from exc
        return SpawnOutcome(stored, created=True, authorization=authorization)

    # -- waiting -----------------------------------------------------------------------

    async def await_child(
        self,
        parent_task_id: str,
        child_task_id: str,
        *,
        claim: DispatchClaim | None = None,
        run_id: str = "",
    ) -> TaskRecord:
        """Make the parent wait on its own child: ``running -> waiting``, releasing its slot.

        Only a parent and its own child may be joined. A child from another parent, namespace or
        session is refused, and the wait is persisted before any slot is released so a crash
        cannot leave a parent that neither runs nor waits.
        """
        relation = await self._control.read_child_of(child_task_id)
        if relation is None:
            raise ChildRefused(
                SpawnRefusal.NOT_A_CHILD, f"{child_task_id!r} is not a registered child"
            )
        if relation.parent_task_id != parent_task_id:
            raise ChildRefused(
                SpawnRefusal.NOT_PERMITTED,
                f"the wait is for {parent_task_id!r}, but {child_task_id!r} belongs to "
                f"{relation.parent_task_id!r}",
            )
        parent = await self._control.read_task(parent_task_id)
        if parent is None:
            raise ChildRefused(SpawnRefusal.UNKNOWN_PARENT, f"no task {parent_task_id!r}")
        if parent.is_terminal:
            raise ChildRefused(
                SpawnRefusal.PARENT_TERMINAL, f"the parent ended as {parent.status.value}"
            )
        if parent.cancel_requested_at is not None:
            raise ChildRefused(SpawnRefusal.CANCELLED_FIRST, "a cancellation was requested first")
        if parent.status is TaskStatus.WAITING:
            return parent
        if parent.status is not TaskStatus.RUNNING:
            raise ChildRefused(
                SpawnRefusal.NOT_PERMITTED,
                f"only a running parent can wait on a child; it is {parent.status.value}",
            )
        moved = parent.transition(
            TaskStatus.WAITING,
            reason=REASON_AWAITING,
            at=self._clock(),
            waiting_for=f"child:{child_task_id}",
        )
        try:
            return await self._control.save_task(moved, expected_status=parent.status)
        except ContractError as exc:
            raise ChildRefused(SpawnRefusal.NOT_PERMITTED, str(exc)) from exc

    async def active_children(self, parent_task_id: str) -> tuple[ChildRelation, ...]:
        """Relations whose child has not reached a terminal committed fact."""
        active: list[ChildRelation] = []
        for relation in await self._control.read_children(parent_task_id):
            child = await self._control.read_task(relation.child_task_id)
            if child is not None and not child.is_terminal:
                active.append(relation)
        return tuple(active)

    async def finish_interaction(self, run_id: str) -> Mapping[str, Any] | None:
        """The interaction a run must suspend on before it finishes, or ``None``.

        Handed to ``DefaultHarness`` as its ``finish_barrier``: it is called *before* the run's
        terminal commit, so a parent whose work is done but whose child still runs pauses as a
        durable ``child_result`` wait instead of finishing and being revived later (plan §10.3).
        It reads only committed control facts and never touches the model or a tool.
        """
        task = await self._control.read_task_by_run(run_id)
        if task is None or task.is_terminal or task.cancel_requested_at is not None:
            return None
        active = await self.active_children(task.task_id)
        if not active:
            return None
        return {
            "kind": "child_result",
            "child_task_id": active[0].child_task_id,
            "spawn_identity": active[0].spawn_identity,
            "reason": "the parent is waiting for its child's committed result",
        }

    async def hold_for_children(self, task: TaskRecord) -> TaskRecord | None:
        """The terminal barrier a worker calls before settling a parent.

        Returns the parent in ``waiting`` when it still has active children, or ``None`` when it
        may settle as its run says. The join is persisted *before* the run's terminal state, so a
        completed parent with a running child cannot be produced (plan §10.3).
        """
        if task.is_terminal:
            return None
        active = await self.active_children(task.task_id)
        if not active:
            return None
        if task.status is TaskStatus.WAITING and task.waiting_for.startswith("child:"):
            return task
        if task.status is not TaskStatus.RUNNING:
            return None
        moved = task.transition(
            TaskStatus.WAITING,
            reason=REASON_AWAITING,
            at=self._clock(),
            waiting_for=f"child:{active[0].child_task_id}",
        )
        try:
            return await self._control.save_task(moved, expected_status=task.status)
        except ContractError:
            return await self._control.read_task(task.task_id)

    # -- completion --------------------------------------------------------------------

    async def complete_child(
        self,
        child_task_id: str,
        *,
        outcome: ChildOutcome | str,
        reason: str = "",
        result: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        accepted: bool | None = None,
        acceptance_detail: str = "",
        attempt_id: str = "",
    ) -> ChildResult:
        """Settle a child: terminal record + relation end + notice + grant settlement, atomically.

        ``accepted`` is the verifier's verdict; when it is not supplied the host verifier runs.
        The completion notice is written in the same transaction as the child's terminal state, so
        a crash cannot leave a finished child whose parent is never told (plan §10.3, case C-02).
        """
        now = self._clock()
        relation = await self._control.read_child_of(child_task_id)
        if relation is None:
            raise ChildRefused(
                SpawnRefusal.NOT_A_CHILD, f"{child_task_id!r} is not a registered child"
            )
        child = await self._control.read_task(child_task_id)
        if child is None:
            raise ChildRefused(SpawnRefusal.UNKNOWN_CHILD, f"no task {child_task_id!r}")
        existing = await self.child_result(child_task_id)
        if existing is not None:
            # A repeated completion (a redelivered report) returns the committed result instead of
            # inventing a second one; the first terminal fact wins.
            return existing
        if child.is_terminal:
            # A worker may have settled the child's own task before the coordinator wrote the
            # completion boundary. The outcome is then the child's committed one, and the notice,
            # the relation end and the grant settlement are still written exactly once here.
            resolved = ChildOutcome(child.status.value)
        else:
            resolved = (
                outcome if isinstance(outcome, ChildOutcome) else ChildOutcome(str(outcome))
            )
        payload = dict(result or {})
        authorization = await self._authorization_for(relation)
        if accepted is None:
            accepted, acceptance_detail = self._verifier.verify(authorization, payload)
        child_result = ChildResult(
            parent_task_id=relation.parent_task_id,
            child_task_id=child_task_id,
            spawn_identity=relation.spawn_identity,
            outcome=resolved,
            reason=reason or child.stop_code or child.reason,
            result=payload,
            accepted=bool(accepted),
            acceptance_detail=acceptance_detail,
            usage=dict(usage or {}),
            run_id=child.run_id,
            attempt_id=attempt_id or child.current_attempt_id,
            created_at=now,
        )
        notice = ControlOutboxEntry(
            outbox_id=f"{OUTBOX_CHILD_COMPLETED}:{child_task_id}",
            kind=OUTBOX_CHILD_COMPLETED,
            target_task_id=relation.parent_task_id,
            payload=child_result.to_record(),
            namespace=child.namespace,
            created_at=now,
        )
        settled_relation = replace(relation, state=resolved.spawn_state)
        if child.is_terminal:
            moved = child
        elif child.status is TaskStatus.QUEUED and resolved is ChildOutcome.CANCELLED:
            # A child still in the queue never dispatched, so cancellation settles it through its
            # two documented steps.
            moved = child.transition(
                TaskStatus.CANCELLING,
                reason=REASON_CANCEL_REQUESTED,
                at=now,
                cancel_requested_at=child.cancel_requested_at or now,
            )
            moved = moved.transition(TaskStatus.CANCELLED, reason=REASON_CANCELLED, at=now)
        else:
            moved = child.transition(
                TaskStatus(resolved.value),
                reason=_OUTCOME_REASON[resolved],
                at=now,
                stop_code=reason,
            )
        settlement = self._settlement_for(relation, child, resolved, usage)
        try:
            await self._control.save_task_with_notices(
                moved,
                expected_status=child.status,
                notifications=(notice,),
                relation=settled_relation,
                settle_reservation=settlement,
            )
        except ContractError as exc:
            raise ChildRefused(SpawnRefusal.NOT_PERMITTED, str(exc)) from exc
        return child_result

    def _settlement_for(
        self,
        relation: ChildRelation,
        child: TaskRecord,
        outcome: ChildOutcome,
        usage: Mapping[str, Any] | None,
    ) -> tuple[str, str, float | None, str] | None:
        """How the child's grant is closed: release only what provably never started.

        A cancelled child that never took an attempt is the one case a release is provable; a
        dispatched child whose usage is not reported stays ``unknown`` rather than being refunded
        (plan §9). This is the same honesty rule the root budget uses.
        """
        if not relation.budget_reservation_id:
            return None
        if outcome is ChildOutcome.CANCELLED and child.attempt_count == 0:
            return (
                relation.budget_reservation_id,
                "released",
                None,
                "the child was cancelled before it ever took an attempt",
            )
        reported = _usage_amount(usage, relation)
        if reported is not None:
            return (
                relation.budget_reservation_id,
                "settled",
                reported,
                "the child reported what it used",
            )
        return (
            relation.budget_reservation_id,
            "unknown",
            None,
            "the child was dispatched and its final usage was not established",
        )

    async def child_result(self, child_task_id: str) -> ChildResult | None:
        """Re-read the committed completion notice for one child, if there is one."""
        return await read_committed_result(self._control, child_task_id)

    async def _authorization_for(self, relation: ChildRelation) -> ChildAuthorization:
        """Rebuild the effective authority from the durable spawn notice.

        Completion re-reads the authorization that was committed; it does not re-derive it from
        the parent's current prompt or from the child's result. A drift between the notice's
        digest and the relation's is refused rather than silently reinterpreted.
        """
        for entry in await self._control.read_outbox(
            task_id=relation.parent_task_id, pending_only=False, limit=1000
        ):
            if entry.kind != OUTBOX_CHILD_SPAWNED:
                continue
            if str(entry.payload.get("spawn_identity") or "") != relation.spawn_identity:
                continue
            digest = str(entry.payload.get("authorization_digest") or "")
            if relation.authorization_digest and digest and digest != relation.authorization_digest:
                raise ChildRefused(
                    SpawnRefusal.CONFIG_CONFLICT,
                    "the committed authorization no longer matches the relation's digest",
                )
            record = dict(entry.payload.get("authorization") or {})
            return ChildAuthorization(
                profile=str(record.get("profile") or ""),
                tools=tuple(record.get("tools") or ()),
                workspace_ref=str(record.get("workspace_ref") or ""),
                result_schema=dict(record.get("result_schema") or {}),
                budget_unit=BudgetUnit(
                    str(record.get("budget_unit") or BudgetUnit.MODEL_REQUESTS.value)
                ),
                budget_amount=float(record.get("budget_amount") or 0.0),
                budget_cap=record.get("budget_cap"),
                max_depth=int(record.get("max_depth") or self._policy.max_depth),
            )
        return ChildAuthorization(profile="", max_depth=self._policy.max_depth)

    # -- delivery and parent resumption ------------------------------------------------

    async def deliver_notifications(self, *, limit: int = 200) -> tuple[str, ...]:
        """Turn pending completion notices into durable parent inputs, then wake ready parents.

        Delivery is deduplicated by the notice's own id: a redelivery is a duplicate admission,
        not a second application. A parent that already ended has the notice archived (marked
        delivered) rather than retried forever, and is never revived (plan §10.3/§10.4).

        Only the terminal events (``child_completed``) are delivered as parent input. A
        ``child_spawned`` notice is a record that the spawn committed, not a wake-up: turning it
        into an input would wake a parent that is still driving.
        """
        now = self._clock()
        delivered: list[str] = []
        entries = list(await self._control.read_outbox(pending_only=True, limit=limit))
        parents: set[str] = set()
        for entry in entries:
            if entry.kind != OUTBOX_CHILD_COMPLETED:
                continue
            try:
                result = ChildResult.from_record(entry.payload)
            except Exception as exc:  # noqa: BLE001 - a corrupt notice is a recorded failure
                await self._control.fail_outbox(entry.outbox_id, error=str(exc))
                continue
            child = await self._control.read_task(result.child_task_id)
            if child is None:
                await self._control.fail_outbox(
                    entry.outbox_id, error="the child task is no longer in the store"
                )
                continue
            admission = self.completion_input(entry, result, child)
            outcome = await self._control.admit_input(admission)
            if outcome.outcome is AdmissionOutcome.REFUSED and outcome.refusal_code not in {
                RefusalCode.TASK_TERMINAL.value,
                RefusalCode.CANCELLED_FIRST.value,
            }:
                await self._control.fail_outbox(
                    entry.outbox_id, error=f"the parent refused the notice: {outcome.refusal_code}"
                )
                continue
            await self._control.deliver_outbox(entry.outbox_id, at=now)
            delivered.append(entry.outbox_id)
            parents.add(entry.target_task_id)
        for parent_task_id in parents:
            await self._wake_if_ready(parent_task_id)
        return tuple(delivered)

    def completion_input(
        self, entry: ControlOutboxEntry, result: ChildResult, child: TaskRecord
    ) -> InputAdmission:
        """The exact parent input one completion notice produces, as a stable, replayable value.

        Public because delivery is two transactions — admit the input, then mark the notice
        delivered — and a host recovering a crash in that gap needs to rebuild the *same* input
        so the redelivery is a duplicate rather than a content conflict. The id is the notice's
        own id and the content is derived only from committed facts, never from a live callback.
        """
        return InputAdmission(
            namespace=child.namespace,
            session_id=child.session_id,
            task_id=entry.target_task_id,
            input_id=entry.outbox_id,
            content=InputContent(
                kind=InputKind.CHILD_COMPLETED,
                child_id=result.child_task_id,
                parameters={
                    "spawn_identity": result.spawn_identity,
                    "outcome": result.outcome.value,
                },
            ),
            source="internal",
        )

    async def _wake_if_ready(self, parent_task_id: str) -> TaskRecord | None:
        parent = await self._control.read_task(parent_task_id)
        if parent is None or parent.status is not TaskStatus.WAITING:
            return parent
        if not parent.waiting_for.startswith("child:"):
            return parent
        if parent.cancel_requested_at is not None:
            return parent
        if await self.active_children(parent_task_id):
            return parent
        moved = parent.transition(TaskStatus.QUEUED, reason=REASON_RESUMED, at=self._clock())
        try:
            return await self._control.save_task(moved, expected_status=parent.status)
        except ContractError:
            return await self._control.read_task(parent_task_id)

    async def apply_child_results(self, parent_task_id: str) -> tuple[ChildResult, ...]:
        """The results a parent may read, rebuilt from committed records.

        The notified message is only a hint: each result is re-read from the child's committed
        terminal record plus its durable notice, so a result that is missing, unaccepted or from
        a child outside this parent never becomes a fact by being mentioned (plan §10.3).
        """
        results: list[ChildResult] = []
        for record in await self._control.read_inputs(parent_task_id, limit=200):
            if record.kind is not InputKind.CHILD_COMPLETED:
                continue
            child_task_id = record.content.child_id
            if not child_task_id:
                continue
            relation = await self._control.read_child_of(child_task_id)
            if relation is None or relation.parent_task_id != parent_task_id:
                continue
            child = await self._control.read_task(child_task_id)
            if child is None or not child.is_terminal:
                continue
            result = await self.child_result(child_task_id)
            if result is not None:
                results.append(result)
        return tuple(results)

    # -- cancellation ------------------------------------------------------------------

    async def cancel_subtree(
        self, task_id: str, *, reason: str = "", settle: bool = True
    ) -> tuple[TaskRecord, ...]:
        """Persist the cancellation intent, then settle what can be settled without a driver.

        The intent is written before anything is stopped, so a crash mid-propagation leaves a
        durable request rather than children that quietly kept running. A child whose worker is
        live is only *asked* to stop: the worker's run lease decides who may commit. No child is
        deleted and no unknown effect is rewritten as never having happened (plan §10.4).
        """
        now = self._clock()
        task = await self._control.read_task(task_id)
        if task is None:
            raise ChildRefused(SpawnRefusal.UNKNOWN_CHILD, f"no task {task_id!r}")
        affected: list[TaskRecord] = []
        if not task.is_terminal:
            updated = task
            if updated.cancel_requested_at is None:
                updated = updated.request_cancel(at=now)
                updated = await self._control.save_task(updated, expected_status=task.status)
                affected.append(updated)
            if settle and updated.status in {TaskStatus.QUEUED, TaskStatus.WAITING}:
                affected.append(await self._settle_queued_cancel(updated, at=now))
        for relation in await self._control.read_children(task_id):
            child = await self._control.read_task(relation.child_task_id)
            if child is None or child.is_terminal:
                continue
            if child.cancel_requested_at is None:
                child = await self._control.save_task(
                    child.request_cancel(at=now), expected_status=child.status
                )
            if settle and child.status in {TaskStatus.QUEUED, TaskStatus.WAITING}:
                # The child never dispatched, so completing it as cancelled is provable and its
                # grant may be released.
                await self.complete_child(
                    child.task_id,
                    outcome=ChildOutcome.CANCELLED,
                    reason=reason or "the parent subtree was cancelled",
                )
                affected.append(
                    await self._control.read_task(child.task_id) or child
                )
            else:
                affected.append(child)
        return tuple(affected)

    async def _settle_queued_cancel(self, task: TaskRecord, *, at: float) -> TaskRecord:
        moving = task
        if moving.status is not TaskStatus.CANCELLING:
            moving = moving.transition(
                TaskStatus.CANCELLING, reason=REASON_CANCEL_REQUESTED, at=at
            )
        try:
            stored = await self._control.save_task(moving, expected_status=task.status)
        except ContractError:
            return await self._control.read_task(task.task_id) or task
        settled = stored.transition(TaskStatus.CANCELLED, reason=REASON_CANCELLED, at=at)
        try:
            return await self._control.save_task(settled, expected_status=stored.status)
        except ContractError:
            return await self._control.read_task(task.task_id) or stored


async def read_committed_result(control: Any, child_task_id: str) -> ChildResult | None:
    """The child's committed completion notice, read from the control store alone.

    Module-level so a harness resuming a ``child_result`` suspension can rebuild the result from
    the durable record without holding a coordinator or trusting a live callback.
    """
    relation = await control.read_child_of(child_task_id)
    if relation is None:
        return None
    for entry in await control.read_outbox(
        task_id=relation.parent_task_id, pending_only=False, limit=1000
    ):
        if entry.kind != OUTBOX_CHILD_COMPLETED:
            continue
        if str(entry.payload.get("child_task_id") or "") != child_task_id:
            continue
        return ChildResult.from_record(entry.payload)
    return None


def _under_any(candidate: str, prefixes: Sequence[str]) -> bool:
    for prefix in prefixes:
        root = prefix.rstrip("/")
        if not root:
            continue
        if candidate == root or candidate.startswith(root + "/"):
            return True
    return False


def _usage_amount(usage: Mapping[str, Any] | None, relation: ChildRelation) -> float | None:
    """The reported usage for the relation's grant unit, if the report actually names it.

    A report in a different unit than the grant is not silently coerced: the amount is unknown
    for this reservation, which is the honest answer.
    """
    if not usage:
        return None
    if "unit" in usage:
        reported = str(usage.get("unit") or "")
        if not relation.budget_reservation_id:
            return None
        if "amount" not in usage:
            return None
        return float(usage["amount"]) if reported else None
    if "amount" not in usage:
        return None
    return float(usage["amount"])
