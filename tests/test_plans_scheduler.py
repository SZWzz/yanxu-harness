from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from yanxu_harness.errors import ContractError

from yanxu_harness.plans import (
    AcceptanceCriterion,
    ApprovalDecision,
    ExecutionEnvelope,
    PlanApprovalRequest,
    PlanDefinition,
    PlanRefused,
    StepDefinition,
    VersionedRef,
)
from yanxu_harness.tasks.inbox import MemoryControlStore, SqliteControlStore
from yanxu_harness.tasks.scheduler import LocalWorker, SchedulerLimits, WorkResult
from yanxu_harness.tasks.types import (
    BacklogLimits,
    InputContent,
    InputKind,
    TaskAdmission,
    TaskStatus,
)


def _definition(*, plan_id: str = "scheduler-plan") -> PlanDefinition:
    return PlanDefinition(
        namespace="ns",
        session_id="session",
        plan_id=plan_id,
        revision=1,
        title="Scheduler plan",
        goal="Run an approved step",
        created_by="host",
        steps=(
            StepDefinition(
                step_id="first",
                title="First step",
                instruction="Do the approved work",
                acceptance_criteria=(AcceptanceCriterion("done", "host.result", "1"),),
            ),
        ),
        execution_envelope=ExecutionEnvelope(
            profile_ref=VersionedRef("profile/default", "1"),
            workspace_ref=VersionedRef("workspace/main", "v1"),
            policy_ref=VersionedRef("policy/default", "1"),
            deployment_ref=VersionedRef("deployment/local", "1"),
            budget_ref=VersionedRef("budget/default", "1"),
        ),
    )


@pytest.fixture(params=("memory", "sqlite"))
def control(request: pytest.FixtureRequest, tmp_path):
    if request.param == "memory":
        store = MemoryControlStore(enable_orchestration=True)
    else:
        store = SqliteControlStore(tmp_path / "plans-scheduler.sqlite", enable_orchestration=True)
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()


async def _approve(
    store,
    definition: PlanDefinition | None = None,
    *,
    expires_at_ms: int | None = None,
):
    definition = definition or _definition()
    record = await store.create_plan(
        definition, request_id=f"create-{definition.plan_id}", principal="host", now=100.0
    )
    record = await store.request_plan_review(
        definition.namespace, definition.plan_id, expected_control_version=record.control_version,
        request_id=f"review-{definition.plan_id}", principal="host", now=101.0,
    )
    return await store.decide_plan_approval(
        definition.namespace, definition.plan_id,
        PlanApprovalRequest(
            decision_id="approved-v1", decision=ApprovalDecision.APPROVED,
            approval_policy_version="policy-v1", expires_at_ms=expires_at_ms,
        ),
        expected_control_version=record.control_version,
        request_id=f"approve-{definition.plan_id}", principal="reviewer", now_ms=102_000,
    )


def _worker_plan_request_id(candidate) -> str:
    identity = {
        "version": 1,
        "namespace": candidate.namespace,
        "plan_id": candidate.plan_id,
        "revision": candidate.revision,
        "step_id": candidate.step_id,
    }
    return "worker-step-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_work_result_keeps_its_public_record_projection() -> None:
    result = WorkResult(
        task_id="task-1",
        namespace="ns",
        outcome="completed",
        detail="done",
        run_id="run-1",
        task_status=TaskStatus.COMPLETED,
        claimed=True,
    )

    assert result.to_record() == {
        "task_id": "task-1",
        "namespace": "ns",
        "outcome": "completed",
        "detail": "done",
        "run_id": "run-1",
        "task_status": "completed",
        "claimed": True,
    }


@pytest.mark.asyncio
async def test_ready_step_is_claimed_atomically_and_hidden_from_ordinary_worker(control) -> None:
    approved = await _approve(control)
    page = await control.read_ready_steps("ns", limit=8, now=103.0)

    assert len(page.candidates) == 1
    candidate = page.candidates[0]
    assert (candidate.namespace, candidate.plan_id, candidate.revision, candidate.step_id) == (
        "ns", "scheduler-plan", 1, "first"
    )
    assert candidate.ready_since == 102.0
    assert candidate.control_version == approved.control_version
    assert page.next_cursor is None

    result = await control.claim_plan_step(
        candidate,
        expected_control_version=approved.control_version,
        request_id="claim-first",
        principal="worker-host",
        holder="worker-1",
        ttl_seconds=30,
        now=103.0,
    )
    from yanxu_harness.plans import step_binding_id

    assert result.binding.binding_id == step_binding_id("ns", "scheduler-plan", 1, "first")
    assert result.task.status is TaskStatus.RUNNING
    assert result.task.profile == "profile/default"
    assert result.task.workspace_ref == "workspace/main"
    assert result.claim.task_id == result.task.task_id
    inputs = await control.read_inputs(result.task.task_id)
    assert len(inputs) == 1
    assert inputs[0].kind is InputKind.INITIAL
    assert "Do the approved work" in inputs[0].content.text
    assert inputs[0].content.parameters["plan_step"] == {
        "plan_id": "scheduler-plan",
        "revision": 1,
        "step_id": "first",
    }
    assert inputs[0].content.parameters["execution_envelope"]["workspace_ref"]["ref"] == "workspace/main"

    assert await control.read_schedulable(limit=10, now=104.0) == ()
    with pytest.raises(PlanRefused) as bypass:
        await control.claim_task(
            result.task.task_id, holder="old-worker", ttl_seconds=30, now=104.0
        )
    assert bypass.value.reason_code == "execution_denied"


@pytest.mark.asyncio
async def test_execution_binding_is_committed_with_plan_task_and_claim(control) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id="first", principal_ref="worker-host", now_ms=103_000
    )
    resolved = ResolvedExecutionBinding(
        request=request,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=ExecutionDeploymentDescriptor(
            profile_fingerprint="sha256:profile-config",
            tool_visibility_fingerprint="sha256:tool-visibility",
            policy_fingerprint="sha256:policy-config",
            deployment_fingerprint="sha256:deployment-config",
            budget_fingerprint="sha256:budget-config",
            workspace_identity="workspace:stable-42",
            workspace_version="tree:rev-9",
            capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
        ),
    )

    admitted = await control.claim_plan_step_with_execution_binding(
        candidate,
        binding_request=request,
        resolved_binding=resolved,
        expected_control_version=approved.control_version,
        request_id="claim-first-bound",
        principal="worker-host",
        holder="worker-1",
        ttl_seconds=30,
        now=103.0,
    )

    stored = await control.read_plan_execution_binding_for_task(admitted.task.task_id)
    assert stored == resolved
    assert stored.request_digest == request.digest
    assert admitted.binding.binding_id == request.binding_id


@pytest.mark.asyncio
async def test_execution_binding_for_a_different_namespace_is_refused_without_admission(control) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        step_binding_id,
    )

    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id=candidate.step_id, principal_ref="worker-host", now_ms=103_000
    )
    wrong_namespace = replace(
        request,
        namespace="other",
        binding_id=step_binding_id("other", request.plan_id, request.revision, request.step_id),
    )
    resolved = ResolvedExecutionBinding(
        request=wrong_namespace,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=ExecutionDeploymentDescriptor(
            profile_fingerprint="sha256:profile-config",
            tool_visibility_fingerprint="sha256:tool-visibility",
            policy_fingerprint="sha256:policy-config",
            deployment_fingerprint="sha256:deployment-config",
            budget_fingerprint="sha256:budget-config",
            workspace_identity="workspace:stable-42",
            workspace_version="tree:rev-9",
            capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
        ),
    )

    with pytest.raises(PlanRefused) as refused:
        await control.claim_plan_step_with_execution_binding(
            candidate,
            binding_request=wrong_namespace,
            resolved_binding=resolved,
            expected_control_version=approved.control_version,
            request_id="claim-wrong-namespace",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=103.0,
        )

    assert refused.value.reason_code == "binding_request_mismatch"
    assert await control.read_plan_bindings("ns", "scheduler-plan") == ()
    assert await control.read_schedulable(limit=8, now=104.0) == ()


@pytest.mark.asyncio
async def test_execution_binding_rolls_back_when_plan_event_commit_fails(control, monkeypatch) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id="first", principal_ref="worker-host", now_ms=103_000
    )
    resolved = ResolvedExecutionBinding(
        request=request,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=ExecutionDeploymentDescriptor(
            profile_fingerprint="sha256:profile-config",
            tool_visibility_fingerprint="sha256:tool-visibility",
            policy_fingerprint="sha256:policy-config",
            deployment_fingerprint="sha256:deployment-config",
            budget_fingerprint="sha256:budget-config",
            workspace_identity="workspace:stable-42",
            workspace_version="tree:rev-9",
            capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
        ),
    )

    def fail_event(_event):
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(control, "_plan_append_event_locked", fail_event)
    with pytest.raises(RuntimeError, match="simulated event write failure"):
        await control.claim_plan_step_with_execution_binding(
            candidate,
            binding_request=request,
            resolved_binding=resolved,
            expected_control_version=approved.control_version,
            request_id="claim-first-bound",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=103.0,
        )

    snapshot = await control.read_plan("ns", "scheduler-plan")
    assert snapshot.record.status.value == "approved"
    assert snapshot.record.control_version == approved.control_version
    assert await control.read_plan_bindings("ns", "scheduler-plan") == ()
    assert await control.read_plan_execution_binding_for_task("plan-task-not-created") is None
    assert await control.read_schedulable(limit=8, now=104.0) == ()
    assert (await control.read_ready_steps("ns", limit=8, now=104.0)).candidates


@pytest.mark.asyncio
async def test_binding_commit_failure_prevents_worker_harness_provider_and_handler(control, monkeypatch) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingGuardResult,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    actual = ExecutionDeploymentDescriptor(
        profile_fingerprint="sha256:profile-config",
        tool_visibility_fingerprint="sha256:tool-visibility",
        policy_fingerprint="sha256:policy-config",
        deployment_fingerprint="sha256:deployment-config",
        budget_fingerprint="sha256:budget-config",
        workspace_identity="workspace:stable-42",
        workspace_version="tree:rev-9",
        capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
    )
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]

    class Resolver:
        async def resolve(self, request):
            return ResolvedExecutionBinding(
                request=request,
                resolver_ref=VersionedRef("resolver/host", "v1"),
                actual=actual,
            )

    class Guard:
        async def check(self, _binding, _actual, *, boundary):
            return ExecutionBindingGuardResult(True)

    def fail_event(_event):
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(control, "_plan_append_event_locked", fail_event)
    provider_calls = []
    handler_calls = []
    harness_calls = []

    class Harness:
        execution_binding_dispatch_supported = True

        async def start(self, run_request, context):
            del run_request
            await context.execution_binding.check_model_dispatch()
            provider_calls.append("provider")
            handler_calls.append("handler")
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    def harness_for(_task, _attempt):
        harness_calls.append(True)
        return Harness()

    worker = LocalWorker(
        control,
        harness_for,
        holder="commit-failure-worker",
        principal="worker-host",
        enable_orchestration=True,
        orchestration_namespace="ns",
        execution_binding_resolver=Resolver(),
        execution_binding_guard=Guard(),
        execution_deployment_for=lambda _harness: actual,
    )

    with pytest.raises(RuntimeError, match="simulated event write failure"):
        await worker.run_once(now=103.0)

    assert harness_calls == []
    assert provider_calls == []
    assert handler_calls == []
    latest = await control.read_plan("ns", "scheduler-plan")
    assert latest.record.status.value == "approved"
    assert latest.record.control_version == approved.control_version
    assert await control.read_plan_bindings("ns", "scheduler-plan") == ()
    assert await control.read_plan_execution_binding_for_task(
        "plan-task-does-not-exist"
    ) is None


@pytest.mark.asyncio
async def test_worker_resolves_and_commits_binding_before_starting_plan_task(control) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingGuardResult,
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    actual = ExecutionDeploymentDescriptor(
        profile_fingerprint="sha256:profile-config",
        tool_visibility_fingerprint="sha256:tool-visibility",
        policy_fingerprint="sha256:policy-config",
        deployment_fingerprint="sha256:deployment-config",
        budget_fingerprint="sha256:budget-config",
        workspace_identity="workspace:stable-42",
        workspace_version="tree:rev-9",
        capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
    )

    class Resolver:
        requests = []

        async def resolve(self, request):
            self.requests.append(request)
            return ResolvedExecutionBinding(
                request=request,
                resolver_ref=VersionedRef("resolver/host", "v1"),
                actual=actual,
            )

    class Guard:
        boundaries = []

        async def check(self, binding, deployment, *, boundary):
            assert binding.actual == deployment == actual
            self.boundaries.append(boundary)
            return ExecutionBindingGuardResult(True)

    class Harness:
        execution_binding_dispatch_supported = True

        async def start(self, request, context):
            del request
            assert context.execution_binding is not None
            await context.execution_binding.check_model_dispatch()
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    await _approve(control)
    resolver = Resolver()
    guard = Guard()
    worker = LocalWorker(
        control,
        lambda _task, _attempt: Harness(),
        holder="bound-worker",
        principal="worker-host",
        enable_orchestration=True,
        orchestration_namespace="ns",
        execution_binding_resolver=resolver,
        execution_binding_guard=guard,
        execution_deployment_for=lambda _harness: actual,
    )

    result = await worker.run_once(now=104.0)

    assert result is not None and result.outcome == "settled:completed"
    assert len(resolver.requests) == 1
    assert resolver.requests[0].principal_ref == "worker-host"
    stored = await control.read_plan_execution_binding_for_task(result.task_id)
    assert stored is not None and stored.request == resolver.requests[0]
    assert guard.boundaries


@pytest.mark.asyncio
async def test_worker_recovery_reuses_persisted_execution_binding_without_resolving(control) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingGuardResult,
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    actual = ExecutionDeploymentDescriptor(
        profile_fingerprint="sha256:profile-config",
        tool_visibility_fingerprint="sha256:tool-visibility",
        policy_fingerprint="sha256:policy-config",
        deployment_fingerprint="sha256:deployment-config",
        budget_fingerprint="sha256:budget-config",
        workspace_identity="workspace:stable-42",
        workspace_version="tree:rev-9",
        capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
    )
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id=candidate.step_id, principal_ref="worker-host", now_ms=103_000
    )
    worker_request_id = _worker_plan_request_id(candidate)
    binding = ResolvedExecutionBinding(
        request=request,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=actual,
    )
    admitted = await control.claim_plan_step_with_execution_binding(
        candidate,
        binding_request=request,
        resolved_binding=binding,
        expected_control_version=approved.control_version,
        request_id=worker_request_id,
        principal="worker-host",
        holder="crashed-worker",
        ttl_seconds=1,
        now=103.0,
    )

    class Resolver:
        async def resolve(self, _request):
            raise AssertionError("recovery must use the persisted binding, not current config")

    class Guard:
        async def check(self, saved, deployment, *, boundary):
            assert saved == binding and deployment == actual
            assert boundary == "model_dispatch"
            return ExecutionBindingGuardResult(True)

    class Harness:
        execution_binding_dispatch_supported = True

        async def start(self, run_request, context):
            del run_request
            assert context.identity.run_id == admitted.task.run_id
            assert context.execution_binding is not None
            await context.execution_binding.check_model_dispatch()
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    worker = LocalWorker(
        control,
        lambda _task, _attempt: Harness(),
        holder="recovery-worker",
        principal="worker-host",
        enable_orchestration=True,
        orchestration_namespace="ns",
        execution_binding_resolver=Resolver(),
        execution_binding_guard=Guard(),
        execution_deployment_for=lambda _harness: actual,
    )

    result = await worker.run_once(now=105.0)

    assert result is not None and result.task_id == admitted.task.task_id
    assert result.outcome == "settled:completed", (
        result.detail,
        await control.read_task(admitted.task.task_id),
    )
    assert await control.read_plan_execution_binding_for_task(admitted.task.task_id) == binding


@pytest.mark.asyncio
async def test_worker_recovery_blocks_changed_deployment_before_model_dispatch_after_revoke(control) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    expected = ExecutionDeploymentDescriptor(
        profile_fingerprint="sha256:profile-config",
        tool_visibility_fingerprint="sha256:tool-visibility",
        policy_fingerprint="sha256:policy-config",
        deployment_fingerprint="sha256:deployment-config",
        budget_fingerprint="sha256:budget-config",
        workspace_identity="workspace:stable-42",
        workspace_version="tree:rev-9",
        capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
    )
    changed = replace(expected, deployment_fingerprint="sha256:deployment-config-new")
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id=candidate.step_id, principal_ref="worker-host", now_ms=103_000
    )
    binding = ResolvedExecutionBinding(
        request=request,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=expected,
    )
    admitted = await control.claim_plan_step_with_execution_binding(
        candidate,
        binding_request=request,
        resolved_binding=binding,
        expected_control_version=approved.control_version,
        request_id=_worker_plan_request_id(candidate),
        principal="worker-host",
        holder="crashed-worker",
        ttl_seconds=1,
        now=103.0,
    )
    current = await control.read_plan("ns", "scheduler-plan")
    await control.revoke_plan_approval(
        "ns", "scheduler-plan", "approved-v1", reason="deployment revoked",
        expected_control_version=current.record.control_version,
        request_id="revoke-before-recovery", principal="host", now_ms=103_500,
    )

    class Resolver:
        async def resolve(self, _request):
            raise AssertionError("recovery must use the saved execution binding")

    class Guard:
        boundaries = []

        async def check(self, _binding, _actual, *, boundary):
            self.boundaries.append(boundary)
            raise AssertionError("descriptor mismatch must refuse before the host guard")

    provider_calls = []
    actual_reads = []

    class Harness:
        execution_binding_dispatch_supported = True

        async def start(self, run_request, context):
            del run_request
            await context.execution_binding.check_model_dispatch()
            provider_calls.append("provider")
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    guard = Guard()
    worker = LocalWorker(
        control,
        lambda _task, _attempt: Harness(),
        holder="recovery-worker",
        principal="worker-host",
        enable_orchestration=True,
        orchestration_namespace="ns",
        execution_binding_resolver=Resolver(),
        execution_binding_guard=guard,
        execution_deployment_for=lambda _harness: actual_reads.append(changed) or changed,
    )

    result = await worker.run_once(now=105.0)

    assert result is not None and result.task_id == admitted.task.task_id
    assert result.outcome == "failed"
    assert "deployment" in result.detail
    assert provider_calls == []
    assert actual_reads == [changed]
    assert guard.boundaries == []
    assert await control.read_plan_execution_binding_for_task(admitted.task.task_id) == binding


@pytest.mark.asyncio
async def test_worker_settles_a_persisted_terminal_run_after_approval_revocation(control) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id=candidate.step_id, principal_ref="worker-host", now_ms=103_000
    )
    binding = ResolvedExecutionBinding(
        request=request,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=ExecutionDeploymentDescriptor(
            profile_fingerprint="sha256:profile-config",
            tool_visibility_fingerprint="sha256:tool-visibility",
            policy_fingerprint="sha256:policy-config",
            deployment_fingerprint="sha256:deployment-config",
            budget_fingerprint="sha256:budget-config",
            workspace_identity="workspace:stable-42",
            workspace_version="tree:rev-9",
            capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
        ),
    )
    admitted = await control.claim_plan_step_with_execution_binding(
        candidate,
        binding_request=request,
        resolved_binding=binding,
        expected_control_version=approved.control_version,
        request_id=_worker_plan_request_id(candidate),
        principal="worker-host",
        holder="crashed-worker",
        ttl_seconds=1,
        now=103.0,
    )
    current = await control.read_plan("ns", "scheduler-plan")
    await control.revoke_plan_approval(
        "ns", "scheduler-plan", "approved-v1", reason="policy changed",
        expected_control_version=current.record.control_version,
        request_id="revoke-before-settlement", principal="host", now_ms=103_500,
    )

    class Resolver:
        async def resolve(self, _request):
            raise AssertionError("a persisted Run must use its committed binding")

    class Guard:
        async def check(self, _binding, _actual, *, boundary):
            raise AssertionError(f"terminal Run settlement must not dispatch at {boundary}")

    class Persistence:
        async def load_run(self, run_id):
            assert run_id == admitted.task.run_id
            return SimpleNamespace(state={"status": "completed", "stop_code": ""})

    class Harness:
        execution_binding_dispatch_supported = True
        _persistence = Persistence()

        async def start(self, _request, _context):
            raise AssertionError("a terminal Run must be settled, not restarted")

        async def resume(self, _run_id, _decisions, _context):
            raise AssertionError("a terminal Run must be settled, not resumed")

    def unexpected_deployment_read(_harness):
        raise AssertionError("no actual deployment check is needed to settle committed facts")

    worker = LocalWorker(
        control,
        lambda _task, _attempt: Harness(),
        holder="settlement-worker",
        principal="worker-host",
        enable_orchestration=True,
        orchestration_namespace="ns",
        execution_binding_resolver=Resolver(),
        execution_binding_guard=Guard(),
        execution_deployment_for=unexpected_deployment_read,
    )

    result = await worker.run_once(now=105.0)

    assert result is not None and result.task_id == admitted.task.task_id
    assert result.outcome == "settled:completed", result.detail
    assert (await control.read_task(admitted.task.task_id)).status is TaskStatus.COMPLETED
    assert await control.read_plan_execution_binding_for_task(admitted.task.task_id) == binding


@pytest.mark.asyncio
async def test_worker_refuses_to_upgrade_an_existing_unbound_plan_task(control) -> None:
    from yanxu_harness.plans import ExecutionBindingRefused

    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    admitted = await control.claim_plan_step(
        candidate,
        expected_control_version=approved.control_version,
        request_id=_worker_plan_request_id(candidate),
        principal="worker-host",
        holder="crashed-worker",
        ttl_seconds=1,
        now=103.0,
    )
    resolved_requests = []
    harness_calls = []

    class Resolver:
        async def resolve(self, request):
            resolved_requests.append(request)
            raise AssertionError("an existing Task cannot be upgraded from current configuration")

    class Guard:
        async def check(self, binding, actual, *, boundary):
            del binding, actual, boundary
            raise AssertionError("missing binding must refuse before dispatch")

    class Harness:
        execution_binding_dispatch_supported = True

        async def start(self, request, context):
            del request, context
            raise AssertionError("missing binding must refuse before starting the Run")

    def harness_for(_task, _attempt):
        harness_calls.append(True)
        return Harness()

    worker = LocalWorker(
        control,
        harness_for,
        holder="recovery-worker",
        principal="worker-host",
        enable_orchestration=True,
        orchestration_namespace="ns",
        execution_binding_resolver=Resolver(),
        execution_binding_guard=Guard(),
        execution_deployment_for=lambda _harness: object(),
    )

    with pytest.raises(ExecutionBindingRefused) as refused:
        await worker.run_once(now=105.0)

    assert refused.value.reason_code == "execution_binding_missing"
    assert resolved_requests == []
    assert harness_calls == []
    assert (await control.read_task(admitted.task.task_id)).status is TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_worker_refuses_unknown_persisted_execution_binding_schema_before_harness(control) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRefused,
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id=candidate.step_id, principal_ref="worker-host", now_ms=103_000
    )
    resolved = ResolvedExecutionBinding(
        request=request,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=ExecutionDeploymentDescriptor(
            profile_fingerprint="sha256:profile-config",
            tool_visibility_fingerprint="sha256:tool-visibility",
            policy_fingerprint="sha256:policy-config",
            deployment_fingerprint="sha256:deployment-config",
            budget_fingerprint="sha256:budget-config",
            workspace_identity="workspace:stable-42",
            workspace_version="tree:rev-9",
            capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
        ),
    )
    admitted = await control.claim_plan_step_with_execution_binding(
        candidate,
        binding_request=request,
        resolved_binding=resolved,
        expected_control_version=approved.control_version,
        request_id=_worker_plan_request_id(candidate),
        principal="worker-host",
        holder="crashed-worker",
        ttl_seconds=1,
        now=103.0,
    )
    if isinstance(control, SqliteControlStore):
        row = control._connection.execute(
            "SELECT record_json FROM plan_execution_bindings WHERE task_id = ?",
            (admitted.task.task_id,),
        ).fetchone()
        corrupt = json.loads(row[0])
        corrupt["schema_version"] = 999
        control._connection.execute(
            "UPDATE plan_execution_bindings SET record_json = ? WHERE task_id = ?",
            (json.dumps(corrupt, sort_keys=True, separators=(",", ":")), admitted.task.task_id),
        )
    else:
        control._plan_execution_binding_records[admitted.task.task_id]["record"][
            "schema_version"
        ] = 999

    harness_calls = []

    class Resolver:
        async def resolve(self, _request):
            raise AssertionError("an existing binding must be read, never re-resolved")

    class Guard:
        async def check(self, binding, actual, *, boundary):
            del binding, actual, boundary
            raise AssertionError("unknown binding schema must refuse before dispatch")

    worker = LocalWorker(
        control,
        lambda _task, _attempt: harness_calls.append(True),
        holder="recovery-worker",
        principal="worker-host",
        enable_orchestration=True,
        orchestration_namespace="ns",
        execution_binding_resolver=Resolver(),
        execution_binding_guard=Guard(),
        execution_deployment_for=lambda _harness: object(),
    )

    with pytest.raises(PlanRefused) as refused:
        await worker.run_once(now=105.0)

    assert refused.value.reason_code == "store_unsupported"
    assert harness_calls == []


@pytest.mark.asyncio
async def test_claim_retry_reuses_binding_task_run_and_initial_input(control) -> None:
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    first = await control.claim_plan_step(
        candidate, expected_control_version=approved.control_version,
        request_id="claim-first", principal="worker-host", holder="worker-1",
        ttl_seconds=30, now=103.0,
    )
    repeated = await control.claim_plan_step(
        candidate, expected_control_version=approved.control_version,
        request_id="claim-first", principal="worker-host", holder="worker-1",
        ttl_seconds=30, now=104.0,
    )

    assert repeated.binding.binding_id == first.binding.binding_id
    assert repeated.task.task_id == first.task.task_id
    assert repeated.task.run_id == first.task.run_id
    assert repeated.claim.fencing_token == first.claim.fencing_token
    assert len(await control.read_inputs(first.task.task_id)) == 1


@pytest.mark.asyncio
async def test_only_enabled_worker_drives_ready_plan_steps_through_harness(control) -> None:
    await _approve(control)
    calls: list[tuple[str, str]] = []

    class Harness:
        async def start(self, request, context):
            del request, context
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    def harness_for(task, attempt_id):
        calls.append((task.task_id, attempt_id))
        return Harness()

    old_worker = LocalWorker(control, harness_for, holder="old", enable_orchestration=False)
    assert await old_worker.run_once(now=103.0) is None
    assert calls == []

    worker = LocalWorker(
        control,
        harness_for,
        holder="plan-worker",
        enable_orchestration=True,
        orchestration_namespace="ns",
    )
    result = await worker.run_once(now=104.0)

    assert result is not None
    assert result.outcome == "settled:completed"
    assert result.run_id.startswith("plan-run-")
    assert calls == [(result.task_id, result.task_id + "-a1")]
    assert await control.read_schedulable(limit=8, now=105.0) == ()


@pytest.mark.asyncio
async def test_worker_merges_ordinary_task_and_ready_plan_under_one_slot(control) -> None:
    await control.admit_task(
        TaskAdmission(
            namespace="ns",
            session_id="session",
            task_id="ordinary-first",
            run_id="ordinary-run",
            initial_input=InputContent(kind=InputKind.INITIAL, text="ordinary work"),
            created_at=90.0,
        )
    )
    await _approve(control)
    starts: list[str] = []

    class Harness:
        async def start(self, request, context):
            del request, context
            starts.append("started")
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    worker = LocalWorker(
        control,
        lambda task, _attempt: Harness(),
        limits=SchedulerLimits(max_concurrent=1, max_per_namespace=1, priority_aging_seconds=0),
        holder="merged-worker",
        enable_orchestration=True,
        orchestration_namespace="ns",
    )
    ordinary = await worker.run_once(now=104.0)
    planned = await worker.run_once(now=105.0)

    assert ordinary is not None and ordinary.task_id == "ordinary-first"
    assert planned is not None and planned.task_id.startswith("plan-task-")
    assert starts == ["started", "started"]
    bindings = await control.read_plan_bindings("ns", "scheduler-plan")
    assert len(bindings) == 1 and bindings[0].task_id == planned.task_id


@pytest.mark.asyncio
async def test_ready_step_reads_are_namespace_scoped_and_worker_requires_explicit_scope(control) -> None:
    await _approve(control)

    assert (await control.read_ready_steps("other-namespace", limit=8, now=103.0)).candidates == ()
    with pytest.raises(ContractError, match="explicit namespace"):
        LocalWorker(control, lambda _task, _attempt: None, enable_orchestration=True)


@pytest.mark.asyncio
async def test_blocked_step_does_not_consume_ready_candidate_limit(control) -> None:
    definition = replace(
        _definition(),
        steps=(
            StepDefinition(
                step_id="a-blocked",
                title="Wait for predecessor",
                instruction="Do not run yet",
                depends_on=("z-ready",),
                acceptance_criteria=(AcceptanceCriterion("done-a", "host.result", "1"),),
            ),
            StepDefinition(
                step_id="z-ready",
                title="Ready first step",
                instruction="Run now",
                acceptance_criteria=(AcceptanceCriterion("done-z", "host.result", "1"),),
            ),
        ),
    )
    await _approve(control, definition)

    page = await control.read_ready_steps("ns", limit=1, now=103.0)

    assert [candidate.step_id for candidate in page.candidates] == ["z-ready"]
    assert page.scanned == 2
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_task_and_binding_roll_back_when_control_event_write_fails(control, monkeypatch) -> None:
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]

    def fail_event(event):
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(control, "_plan_append_event_locked", fail_event)
    with pytest.raises(RuntimeError, match="simulated event write failure"):
        await control.claim_plan_step(
            candidate,
            expected_control_version=approved.control_version,
            request_id="claim-first",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=103.0,
        )

    snapshot = await control.read_plan("ns", "scheduler-plan")
    assert snapshot.record.status.value == "approved"
    assert snapshot.record.control_version == approved.control_version
    assert await control.read_schedulable(limit=8, now=104.0) == ()
    assert (await control.read_ready_steps("ns", limit=8, now=104.0)).candidates


@pytest.mark.asyncio
async def test_two_sqlite_workers_admit_only_one_identity_for_the_same_step(tmp_path) -> None:
    path = tmp_path / "competing-plan-claims.sqlite"
    first = SqliteControlStore(path, enable_orchestration=True)
    second = SqliteControlStore(path, enable_orchestration=True)
    try:
        approved = await _approve(first)
        candidate = (await first.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]

        outcomes = await asyncio.gather(
            first.claim_plan_step(
                candidate,
                expected_control_version=approved.control_version,
                request_id="claim-worker-a",
                principal="host",
                holder="worker-a",
                ttl_seconds=30,
                now=103.0,
            ),
            second.claim_plan_step(
                candidate,
                expected_control_version=approved.control_version,
                request_id="claim-worker-b",
                principal="host",
                holder="worker-b",
                ttl_seconds=30,
                now=103.0,
            ),
            return_exceptions=True,
        )

        winners = [item for item in outcomes if not isinstance(item, Exception)]
        refusals = [item for item in outcomes if isinstance(item, PlanRefused)]
        assert len(winners) == 1
        assert len(refusals) == 1
        bindings = await first.read_plan_bindings("ns", "scheduler-plan")
        assert len(bindings) == 1
        assert bindings[0].binding_id == winners[0].binding.binding_id
        assert len(await first.read_inputs(bindings[0].task_id)) == 1
        assert sum(event.event_type == "step_admitted" for event in await first.read_plan_events("ns", "scheduler-plan")) == 1
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("closing_action", ("pause", "replan", "revoke"))
async def test_execution_bound_claim_races_with_control_closure_at_one_sqlite_commit_boundary(
    tmp_path, closing_action: str
) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    path = tmp_path / f"claim-{closing_action}-race.sqlite"
    first = SqliteControlStore(path, enable_orchestration=True)
    second = SqliteControlStore(path, enable_orchestration=True)
    try:
        approved = await _approve(first)
        candidate = (await first.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
        snapshot = await first.read_plan("ns", "scheduler-plan")
        request = ExecutionBindingRequest.from_snapshot(
            snapshot, step_id=candidate.step_id, principal_ref="host", now_ms=103_000
        )
        resolved = ResolvedExecutionBinding(
            request=request,
            resolver_ref=VersionedRef("resolver/host", "v1"),
            actual=ExecutionDeploymentDescriptor(
                profile_fingerprint="sha256:profile-config",
                tool_visibility_fingerprint="sha256:tool-visibility",
                policy_fingerprint="sha256:policy-config",
                deployment_fingerprint="sha256:deployment-config",
                budget_fingerprint="sha256:budget-config",
                workspace_identity="workspace:stable-42",
                workspace_version="tree:rev-9",
                capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
            ),
        )

        async def close_gate():
            if closing_action == "pause":
                return await second.pause_plan(
                    "ns", "scheduler-plan", reason="race close",
                    expected_control_version=approved.control_version,
                    request_id="close-pause", principal="host", now=103.0,
                )
            if closing_action == "replan":
                return await second.request_replan(
                    "ns", "scheduler-plan", reason="race close",
                    expected_control_version=approved.control_version,
                    request_id="close-replan", principal="host", now=103.0,
                )
            return await second.revoke_plan_approval(
                "ns", "scheduler-plan", "approved-v1", reason="race close",
                expected_control_version=approved.control_version,
                request_id="close-revoke", principal="host", now_ms=103_000,
            )

        claim_outcome, close_outcome = await asyncio.gather(
            first.claim_plan_step_with_execution_binding(
                candidate,
                binding_request=request,
                resolved_binding=resolved,
                expected_control_version=approved.control_version,
                request_id="race-claim",
                principal="host",
                holder="worker-a",
                ttl_seconds=30,
                now=103.0,
            ),
            close_gate(),
            return_exceptions=True,
        )
        successful = [
            item for item in (claim_outcome, close_outcome) if not isinstance(item, Exception)
        ]
        assert len(successful) == 1, (claim_outcome, close_outcome)
        bindings = await first.read_plan_bindings("ns", "scheduler-plan")
        if not isinstance(claim_outcome, Exception):
            assert len(bindings) == 1
            assert bindings[0].task_id == claim_outcome.task.task_id
            assert bindings[0].run_id == claim_outcome.binding.run_id
            assert (await first.read_plan("ns", "scheduler-plan")).record.status.value == "executing"
            assert await first.read_plan_execution_binding_for_task(
                claim_outcome.task.task_id
            ) == resolved
            assert sum(
                event.event_type == "step_admitted"
                for event in await first.read_plan_events("ns", "scheduler-plan")
            ) == 1
        else:
            assert bindings == ()
            plan = await first.read_plan("ns", "scheduler-plan")
            expected_status = "replanning" if closing_action == "replan" else "paused"
            assert plan.record.status.value == expected_status
            assert await first.read_schedulable(limit=8, now=104.0) == ()
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("closing_action", ("pause", "revoke"))
async def test_pause_or_revocation_allows_recovery_of_an_admitted_execution_binding(
    control, closing_action: str
) -> None:
    from yanxu_harness.plans import (
        ExecutionBindingRequest,
        ExecutionDeploymentDescriptor,
        ResolvedExecutionBinding,
        VersionedRef,
    )

    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    snapshot = await control.read_plan("ns", "scheduler-plan")
    request = ExecutionBindingRequest.from_snapshot(
        snapshot, step_id=candidate.step_id, principal_ref="worker-host", now_ms=103_000
    )
    resolved = ResolvedExecutionBinding(
        request=request,
        resolver_ref=VersionedRef("resolver/host", "v1"),
        actual=ExecutionDeploymentDescriptor(
            profile_fingerprint="sha256:profile-config",
            tool_visibility_fingerprint="sha256:tool-visibility",
            policy_fingerprint="sha256:policy-config",
            deployment_fingerprint="sha256:deployment-config",
            budget_fingerprint="sha256:budget-config",
            workspace_identity="workspace:stable-42",
            workspace_version="tree:rev-9",
            capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
        ),
    )
    admitted = await control.claim_plan_step_with_execution_binding(
        candidate,
        binding_request=request,
        resolved_binding=resolved,
        expected_control_version=approved.control_version,
        request_id="claim-first",
        principal="worker-host",
        holder="worker-1",
        ttl_seconds=1,
        now=103.0,
    )
    current = await control.read_plan("ns", "scheduler-plan")
    if closing_action == "pause":
        closed = await control.pause_plan(
            "ns", "scheduler-plan", reason="host pause",
            expected_control_version=current.record.control_version,
            request_id="pause", principal="host", now=103.5,
        )
    else:
        closed = await control.revoke_plan_approval(
            "ns", "scheduler-plan", "approved-v1", reason="policy changed",
            expected_control_version=current.record.control_version,
            request_id="revoke", principal="host", now_ms=103_500,
        )
        assert closed.status.value == "paused"
    await control.release_task_claim(
        admitted.task.task_id,
        holder="worker-1",
        attempt_id=admitted.claim.attempt_id,
        now=103.6,
    )

    page = await control.read_ready_steps("ns", limit=8, now=103.7)
    assert [(item.plan_id, item.step_id) for item in page.candidates] == [
        ("scheduler-plan", "first")
    ]
    recovered = await control.claim_plan_step_with_execution_binding(
        page.candidates[0],
        binding_request=request,
        resolved_binding=resolved,
        expected_control_version=closed.control_version,
        request_id="claim-first",
        principal="worker-host",
        holder="worker-2",
        ttl_seconds=30,
        now=103.7,
    )
    assert recovered.task.task_id == admitted.task.task_id
    assert recovered.task.run_id == admitted.task.run_id
    assert recovered.claim.fencing_token > admitted.claim.fencing_token
    assert await control.read_plan_execution_binding_for_task(admitted.task.task_id) == resolved


@pytest.mark.asyncio
async def test_expired_approval_and_plan_deadline_refuse_step_admission(control) -> None:
    await _approve(control, expires_at_ms=103_000)
    candidate = (await control.read_ready_steps("ns", limit=8, now=102.5)).candidates[0]
    with pytest.raises(PlanRefused) as expired:
        await control.claim_plan_step(
            candidate,
            expected_control_version=candidate.control_version,
            request_id="claim-first",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=104.0,
        )
    assert expired.value.reason_code == "approval_expired"
    assert await control.read_schedulable(limit=8, now=104.0) == ()

    await _approve(control, replace(_definition(plan_id="deadline-plan"), deadline_at_ms=103_000))
    deadline_candidate = (
        await control.read_ready_steps("ns", limit=8, now=102.5)
    ).candidates
    deadline_candidate = next(item for item in deadline_candidate if item.plan_id == "deadline-plan")
    with pytest.raises(PlanRefused) as deadline:
        await control.claim_plan_step(
            deadline_candidate,
            expected_control_version=deadline_candidate.control_version,
            request_id="claim-deadline",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=104.0,
        )
    assert deadline.value.reason_code == "deadline_exceeded"


@pytest.mark.asyncio
async def test_zero_plan_execution_limit_refuses_all_step_admission(control) -> None:
    definition = replace(_definition(), max_step_executions=0)
    await _approve(control, definition)
    assert (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates == ()
    assert await control.read_plan_bindings("ns", "scheduler-plan") == ()
    assert await control.read_schedulable(limit=8, now=104.0) == ()


@pytest.mark.asyncio
async def test_same_worker_cannot_drive_an_idempotent_claim_twice(control, monkeypatch) -> None:
    await _approve(control)
    starts = 0
    both_selected = asyncio.Event()
    run_started = asyncio.Event()
    finish_run = asyncio.Event()
    arrivals = 0

    class Harness:
        async def start(self, request, context):
            nonlocal starts
            del request, context
            starts += 1
            run_started.set()
            await finish_run.wait()
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    worker = LocalWorker(
        control,
        lambda _task, _attempt: Harness(),
        holder="one-worker",
        enable_orchestration=True,
        orchestration_namespace="ns",
    )
    original_choose = worker._choose

    async def competing_choose(at):
        nonlocal arrivals
        decision = await original_choose(at)
        arrivals += 1
        if arrivals == 2:
            both_selected.set()
        await both_selected.wait()
        return decision

    monkeypatch.setattr(worker, "_choose", competing_choose)
    first = asyncio.create_task(worker.run_once(now=103.0))
    second = asyncio.create_task(worker.run_once(now=103.0))
    await asyncio.wait_for(run_started.wait(), timeout=2)
    done, _pending = await asyncio.wait(
        {first, second}, timeout=2, return_when=asyncio.FIRST_COMPLETED
    )
    assert len(done) == 1
    assert next(iter(done)).result().outcome == "already_claimed_here"
    finish_run.set()
    results = await asyncio.gather(first, second)

    assert starts == 1
    assert sum(item is not None and item.outcome == "settled:completed" for item in results) == 1
    assert sum(item is not None and item.outcome == "already_claimed_here" for item in results) == 1


@pytest.mark.asyncio
async def test_plan_claim_obeys_existing_task_input_backlog(control) -> None:
    control.backlog = BacklogLimits(total=1)
    await control.admit_task(
        TaskAdmission(
            namespace="ns",
            session_id="session",
            task_id="ordinary-task",
            run_id="ordinary-run",
            initial_input=InputContent(kind=InputKind.INITIAL, text="already using capacity"),
            created_at=99.0,
        )
    )
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]

    with pytest.raises(PlanRefused) as full:
        await control.claim_plan_step(
            candidate,
            expected_control_version=approved.control_version,
            request_id="claim-first",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=103.0,
        )

    assert full.value.reason_code == "backlog_full"
    snapshot = await control.read_plan("ns", "scheduler-plan")
    assert snapshot.record.status.value == "approved"
    assert snapshot.record.control_version == approved.control_version
    assert [item.task_id for item in await control.read_schedulable(limit=8, now=104.0)] == [
        "ordinary-task"
    ]
    assert (await control.read_ready_steps("ns", limit=8, now=104.0)).candidates
