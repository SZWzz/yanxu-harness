"""AC29：真实 ChildCoordinator + TaskBudget + Runner 下的委派子集与预算/期限边界。

这里没有替身编排：真 `ChildCoordinator`（读配置授权，不读模型自述）、真 `BudgetLedger` /
`TaskBudget`、真 `DispatchBudget`、真 `DefaultHarness`，工具则是真实 MCP 子进程经
Gateway 的那一套定义。断言三件事：

1. child 只能拿到**委派子集**：子集外的工具在 spawn 前就被拒，子集内的工具在组装时
   也只会得到被委派的那几个（连 registry 里都不存在别的名字）；
2. 额度不被提案放宽：超过宿主上限的 grant 在授权期就被拒，实际 grant 等于申请值；
3. **未知结果与等待不刷新父子额度/期限**：一次落在 unknown 窗口的远端调用（以及一次
   等待审批的调用）不会多出 reservation、不会抬高已用额度、不会改动任何一方的 deadline；
   同一个派发身份重复预留是幂等的。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from durable_edit import DeploymentPaths, ScriptedStream

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from yanxu_harness.core.types import AssistantMessage, StopReason, ToolCall  # noqa: E402
from yanxu_harness.errors import ContractError  # noqa: E402
from yanxu_harness.integrations.mcp import (  # noqa: E402
    McpConfigurationError,
    McpToolBinding,
    build_definitions,
)
from yanxu_harness.runtime import DefaultHarness, Principal, RunContext, RunIdentity, RunRequest, RunStatus  # noqa: E402
from yanxu_harness.storage import SqliteExecutionBackend  # noqa: E402
from yanxu_harness.tasks import (  # noqa: E402
    BudgetLedger,
    BudgetUnit,
    ChildCoordinator,
    ChildPolicy,
    ChildSpawnRequest,
    DispatchBudget,
    InputContent,
    InputKind,
    SqliteControlStore,
    SpawnRefusal,
    TaskAdmission,
    TaskBudget,
)
from yanxu_harness.tools import (  # noqa: E402
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)
from support.deployment import build_deployment  # noqa: E402
from support.harness import facts_of  # noqa: E402

NAMESPACE = "project-a"
SESSION = "s1"

READ = McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))
SLOW = McpToolBinding("slow", effects=frozenset({ToolEffect.READ}))
WRITE = McpToolBinding(
    "bump_then_die",
    effects=frozenset({ToolEffect.WRITE}),
    preview=lambda _ctx, args: {"key": args.get("key"), "effect": "increment"},
)


def context(run_id: str) -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace=NAMESPACE, session_id=SESSION, run_id=run_id, attempt_id="a1"),
        principal=Principal(subject="child", permissions=frozenset({"mcp.write"})),
    )


async def admit(store, task_id: str, *, deadline_at: float | None = None):
    return await store.admit_task(
        TaskAdmission(
            namespace=NAMESPACE,
            session_id=SESSION,
            task_id=task_id,
            run_id=f"run-{task_id}",
            initial_input=InputContent(kind=InputKind.INITIAL, text=f"do {task_id}"),
            profile="worker",
            deadline_at=deadline_at,
        )
    )


def spawn_request(
    *,
    child: str,
    identity: str,
    tools: tuple[str, ...],
    amount: float = 2.0,
    deadline_at: float | None = None,
) -> ChildSpawnRequest:
    return ChildSpawnRequest(
        parent_task_id="parent",
        namespace=NAMESPACE,
        session_id=SESSION,
        spawn_identity=identity,
        child_task_id=child,
        child_run_id=f"run-{child}",
        initial_input=InputContent(kind=InputKind.INITIAL, text="child work"),
        profile="researcher",
        tools=tools,
        budget_amount=amount,
        deadline_at=deadline_at,
    )


def child_tool_specs(definitions, *, registry_path: Path, run_id: str, policy):
    gateway = ToolGateway(
        registry=ToolRegistry(list(definitions)),
        policy=policy,
        approvals=InMemoryApprovalStore(),
        backend=SqliteExecutionBackend(registry_path),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: context(run_id))
    return [core_tool(definition, bindings) for definition in definitions]


async def test_the_child_gets_only_the_delegated_subset_and_unknown_changes_nothing(tmp_path) -> None:
    deploy = await build_deployment(tmp_path, {"echo": READ, "bump_then_die": WRITE, "slow": SLOW})
    snapshot = deploy["snapshot"]
    echo_alias = snapshot.entry("echo").alias
    write_alias = snapshot.entry("bump_then_die").alias
    slow_alias = snapshot.entry("slow").alias

    paths = DeploymentPaths.under(tmp_path / "state")
    store = SqliteControlStore(paths.runs)
    ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 10.0}))
    coordinator = ChildCoordinator(
        store,
        policy=ChildPolicy(
            profiles=frozenset({"researcher"}),
            tools=frozenset({echo_alias, write_alias}),
            workspace_root="/work",
            budget_units={"model_requests": 10.0},
        ),
        ledger=ledger,
    )
    try:
        parent_deadline = time.time() + 600
        await admit(store, "parent", deadline_at=parent_deadline)
        parent = await store.read_task("parent")
        assert parent is not None

        # 1. a tool outside the delegated scope is refused before anything is written.
        with pytest.raises(Exception) as outside:
            await coordinator.spawn(spawn_request(child="child-slow", identity="spawn:slow", tools=(slow_alias,)))
        assert SpawnRefusal.NOT_PERMITTED.value in str(outside.value)
        assert await store.read_task("child-slow") is None, "a refusal creates nothing"
        assert await store.read_reservations("parent") == ()

        # 2. a grant above the host's ceiling is refused during authorization.
        with pytest.raises(Exception) as over:
            await coordinator.spawn(
                spawn_request(child="child-over", identity="spawn:over", tools=(echo_alias,), amount=20.0)
            )
        assert SpawnRefusal.BUDGET_REFUSED.value in str(over.value)
        assert await store.read_task("child-over") is None

        # 3. the delegated subset is authorized exactly, and assembling the child's tools from
        #    the frozen catalog yields exactly that subset — nothing else is even reachable.
        child_deadline = time.time() + 300
        spawn_outcome = await coordinator.spawn(
            spawn_request(
                child="child", identity="spawn:1", tools=(echo_alias, write_alias), deadline_at=child_deadline
            )
        )
        child = await store.read_task("child")
        assert child is not None and child.deadline_at == child_deadline
        # The authorization *is* the delegated subset — not the proposal, and not the catalog.
        authorization = spawn_outcome.authorization
        assert set(authorization.tools) == {echo_alias, write_alias}
        assert authorization.budget_amount == 2.0 and authorization.budget_cap == 10.0
        definitions = build_definitions(
            snapshot,
            lambda entry: (lambda _ctx, _args: {"content": [{"type": "text", "text": "stub"}]}),
            only=authorization.tools,
        )
        names = [definition.name for definition in definitions]
        assert set(names) == set(authorization.tools) and len(names) == len(authorization.tools), names
        # A name that is not in the frozen catalog at all is refused rather than silently dropped.
        with pytest.raises(McpConfigurationError):
            build_definitions(snapshot, lambda entry: (lambda _ctx, _args: None), only=["mcp.nope.missing.000000000000"])
        registry = ToolRegistry(list(definitions))
        with pytest.raises(ContractError):
            registry.get(slow_alias)

        # 4. the child's grant is a real reservation, and the dispatches stay inside it.
        grants = [
            row
            for row in await store.read_reservations("parent", unit=BudgetUnit.MODEL_REQUESTS)
            if row.dispatch_identity == "grant:spawn:1"
        ]
        assert [row.amount for row in grants] == [2.0]
        used_before = await ledger.used("parent", BudgetUnit.MODEL_REQUESTS)
        available_before = await ledger.available("parent", BudgetUnit.MODEL_REQUESTS)
        reservations_before = await store.read_reservations("parent", unit=BudgetUnit.MODEL_REQUESTS)

        # 5. a real child run whose delegated MCP call lands in the unknown window suspends, and
        #    the suspension refreshes neither budget nor deadline.
        write_definitions = list(deploy["adapter"].definitions(only=[write_alias]))
        assert [definition.name for definition in write_definitions] == [write_alias]
        wait_definitions = list(deploy["adapter"].definitions(only=[write_alias]))
        dispatch_budget = DispatchBudget(ledger, root_task_id="parent", task_id="child")
        child_harness = DefaultHarness(
            stream_fn=ScriptedStream(
                AssistantMessage(
                    content=[ToolCall(id="call-1", name=write_alias, arguments={"key": "child"})],
                    stop_reason=StopReason.TOOL_USE,
                )
            ),
            tools=child_tool_specs(
                write_definitions,
                registry_path=tmp_path / "child-operations.sqlite",
                run_id="run-child",
                policy=ConservativePolicy(auto_write=True),
            ),
            persistence=store,
            control=store,
            task_id="child",
            budget=dispatch_budget,
            system_prompt="be brief",
        )
        outcome = await child_harness.start(RunRequest(prompt="child work"), context("run-child"))
        assert outcome.status is RunStatus.SUSPENDED, outcome
        # The suspension *is* the unknown window: the remote wrote its business record, the
        # answer never came back, and the child's own ledger says unknown (not failed).
        child_backend = SqliteExecutionBackend(tmp_path / "child-operations.sqlite")
        child_rows = child_backend.all_operations()
        assert [row["state"] for row in child_rows] == ["unknown"], child_rows
        assert harness.facts_of(deploy["facts"], "server.business_written"), "the remote effect happened"

        child_after = await store.read_task("child")
        parent_after = await store.read_task("parent")
        assert child_after is not None and child_after.deadline_at == child_deadline
        assert parent_after is not None and parent_after.deadline_at == parent_deadline

        reservations_after = await store.read_reservations("parent", unit=BudgetUnit.MODEL_REQUESTS)
        assert len(reservations_after) == len(reservations_before) + 1, "exactly one dispatch reservation"
        used_after = await ledger.used("parent", BudgetUnit.MODEL_REQUESTS)
        assert used_after == used_before + 1
        assert await ledger.available("parent", BudgetUnit.MODEL_REQUESTS) == available_before - 1

        # The unknown result grants nothing: repeating the same dispatch identity is idempotent.
        dispatch = next(
            row for row in reservations_after if row.dispatch_identity.startswith("request:")
        )
        repeated = await ledger.reserve(
            root_task_id="parent",
            task_id="child",
            unit=BudgetUnit.MODEL_REQUESTS,
            amount=dispatch.amount,
            dispatch_identity=dispatch.dispatch_identity,
            reservation_id=dispatch.reservation_id,
        )
        assert repeated.reservation_id == dispatch.reservation_id
        assert len(await store.read_reservations("parent", unit=BudgetUnit.MODEL_REQUESTS)) == len(
            reservations_after
        )
        assert await ledger.used("parent", BudgetUnit.MODEL_REQUESTS) == used_after

        # 6. the same invariants hold for a *waiting* child: an approval pause is not a refresh.
        await coordinator.spawn(
            spawn_request(child="child-wait", identity="spawn:2", tools=(echo_alias, write_alias))
        )
        waiting_harness = DefaultHarness(
            stream_fn=ScriptedStream(
                AssistantMessage(
                    content=[ToolCall(id="call-1", name=write_alias, arguments={"key": "waiting"})],
                    stop_reason=StopReason.TOOL_USE,
                )
            ),
            tools=child_tool_specs(
                wait_definitions,
                registry_path=tmp_path / "child-wait-operations.sqlite",
                run_id="run-child-wait",
                policy=ConservativePolicy(),  # writes need an approval decision
            ),
            persistence=store,
            control=store,
            task_id="child-wait",
            budget=DispatchBudget(ledger, root_task_id="parent", task_id="child-wait"),
            system_prompt="be brief",
        )
        waiting_outcome = await waiting_harness.start(
            RunRequest(prompt="child work"), context("run-child-wait")
        )
        assert waiting_outcome.status is RunStatus.SUSPENDED
        # Waiting for an approval leaves no intent and no receipt at all: nothing was granted,
        # nothing was sent, and the allowance above is the only thing that changed.
        wait_backend = SqliteExecutionBackend(tmp_path / "child-wait-operations.sqlite")
        assert wait_backend.all_operations() == ()
        before_wait = len(reservations_after) + 1
        after_wait = await store.read_reservations("parent", unit=BudgetUnit.MODEL_REQUESTS)
        assert len(after_wait) == before_wait + 1, "one dispatch for the waiting child, no top-up"
        assert (await store.read_task("child-wait")).deadline_at is None
        assert (await store.read_task("parent")).deadline_at == parent_deadline
        assert (await store.read_task("child")).deadline_at == child_deadline
    finally:
        store.close()
        await deploy["manager"].__aexit__(None, None, None)
