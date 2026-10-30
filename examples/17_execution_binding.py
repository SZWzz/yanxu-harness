"""Neutral host wiring for approved Plan execution bindings.

Run from the repository root with ``python examples/17_execution_binding.py``.
The example keeps the legacy independent Run and ordinary Task entry points, then
approves one Plan step under profile A, persists its binding through SQLite, exits
the worker process while the Run is suspended, refuses a changed deployment, and
recovers under the original approved workspace/profile through the real Gateway.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from yanxu_harness.core.types import TextContent
from yanxu_harness.plans import (
    AcceptanceCriterion,
    ExecutionBindingDispatch,
    ExecutionBindingGuardResult,
    ExecutionBindingRefused,
    ExecutionBindingRequest,
    ExecutionDeploymentDescriptor,
    ExecutionEnvelope,
    PlanDefinition,
    ResolvedExecutionBinding,
    StepDefinition,
    VersionedRef,
)
from yanxu_harness.providers import CoreStreamBridge
from yanxu_harness.providers.protocol import (
    Provider,
    ProviderEvent,
    ProviderEventKind,
    ProviderRequest,
)
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks import (
    InputAdmission,
    InputContent,
    InputKind,
    LocalWorker,
    SchedulerLimits,
    SqliteControlStore,
    TaskAdmission,
)
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

from plan_dag_support import (
    NAMESPACE,
    PRINCIPAL,
    SESSION_ID,
    acceptance_coordinator,
    approve_plan,
    deliver_plan_notices,
)


PLAN_ID = "execution-binding-reference"
SOURCE_V1 = "Profile A's approved workspace snapshot.\n"
SOURCE_V2 = "A changed workspace must not inherit the old approval.\n"


def _append(path: Path, value: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(value + "\n")


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


class CompleteProvider(Provider):
    """A fixed text answer used by the two pre-binding compatibility entries."""

    name = "fixed-neutral-host-provider"

    async def stream(
        self, request: ProviderRequest, cancellation: Any = None
    ) -> AsyncIterator[ProviderEvent]:
        del request, cancellation
        yield ProviderEvent(
            kind=ProviderEventKind.TEXT_DELTA, sequence=0, text="legacy entry completed"
        )
        yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="stop")


class ReadThenAnswerProvider(Provider):
    """Ask for the one declared read, then report the saved tool result."""

    name = "fixed-bound-plan-provider"

    def __init__(self, call_log: Path) -> None:
        self._call_log = call_log

    async def stream(
        self, request: ProviderRequest, cancellation: Any = None
    ) -> AsyncIterator[ProviderEvent]:
        del cancellation
        _append(self._call_log, "provider-dispatch")
        if any(message.get("role") == "tool" for message in request.messages):
            yield ProviderEvent(
                kind=ProviderEventKind.TEXT_DELTA,
                sequence=0,
                text="The approved source was read and recorded.",
            )
            yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="stop")
            return
        yield ProviderEvent(
            kind=ProviderEventKind.TOOL_CALL,
            sequence=0,
            tool_call_id="read-approved-source",
            tool_name="notes.read",
            arguments_json=json.dumps({"path": "source.txt"}),
        )
        yield ProviderEvent(
            kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="tool_calls"
        )


def _workspace_version(workspace: Path) -> str:
    return "sha256:" + hashlib.sha256((workspace / "source.txt").read_bytes()).hexdigest()


def _descriptor(workspace: Path, profile: str) -> ExecutionDeploymentDescriptor:
    if profile not in {"A", "B"}:
        raise ValueError(f"unknown profile {profile!r}")
    return ExecutionDeploymentDescriptor(
        profile_fingerprint=f"sha256:profile-{profile.lower()}-config",
        tool_visibility_fingerprint="sha256:notes-read-only-v1",
        policy_fingerprint="sha256:read-policy-v1",
        deployment_fingerprint=f"sha256:neutral-host-profile-{profile.lower()}",
        budget_fingerprint="sha256:offline-budget-v1",
        workspace_identity="workspace:execution-binding-demo",
        workspace_version=_workspace_version(workspace),
        capabilities=("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
    )


class StaticResolver:
    """Resolve only the approved profile/workspace versions supplied by the Plan."""

    def __init__(self, workspace: Path, profile: str) -> None:
        self._workspace = workspace
        self._profile = profile

    async def resolve(self, request: ExecutionBindingRequest) -> ResolvedExecutionBinding:
        actual = _descriptor(self._workspace, self._profile)
        if request.references.profile_ref.version != self._profile:
            raise ExecutionBindingRefused(
                "profile_reference_unavailable", "the approved profile version is not available"
            )
        if request.references.workspace_ref.version != actual.workspace_version:
            raise ExecutionBindingRefused(
                "workspace_version_unavailable", "the approved workspace snapshot is not available"
            )
        return ResolvedExecutionBinding(
            request=request,
            resolver_ref=VersionedRef("resolver/neutral-host", "v1"),
            actual=actual,
        )


class AllowBoundDeployment:
    """A host guard for a deployment already checked against the persisted descriptor."""

    async def check(self, binding: Any, actual: Any, *, boundary: Any) -> ExecutionBindingGuardResult:
        del boundary
        if binding.actual != actual:
            return ExecutionBindingGuardResult(
                False,
                "actual_deployment_mismatch",
                "the running deployment differs from the persisted Plan binding",
            )
        return ExecutionBindingGuardResult(True)


def _read_tool(workspace: Path, handler_log: Path) -> ToolDefinition:
    root = workspace.resolve()

    def validate(arguments: Mapping[str, Any]) -> dict[str, str]:
        path = arguments.get("path")
        if path != "source.txt":
            raise ValueError("this deployment exposes only source.txt")
        return {"path": path}

    def handler(_context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        path = (root / arguments["path"]).resolve()
        if path.parent != root:
            raise ValueError("path escaped the approved workspace")
        content = path.read_text(encoding="utf-8")
        _append(handler_log, "gateway-handler")
        return {
            "content": [TextContent(text=content)],
            "details": {
                "path": arguments["path"],
                "workspace_version": _workspace_version(root),
            },
        }

    return ToolDefinition(
        "notes.read",
        "read the one source file exposed by the approved workspace",
        effects=frozenset({ToolEffect.READ}),
        args_validator=validate,
        handler=handler,
    )


def _plan_definition(workspace: Path) -> PlanDefinition:
    workspace_version = _workspace_version(workspace)
    return PlanDefinition(
        namespace=NAMESPACE,
        session_id=SESSION_ID,
        plan_id=PLAN_ID,
        revision=1,
        title="Read one approved source snapshot",
        goal="Demonstrate host binding and process recovery without widening tool access.",
        steps=(
            StepDefinition(
                step_id="inspect",
                title="Inspect the approved source",
                instruction="Read source.txt using notes.read and report what it contains.",
                acceptance_criteria=(
                    AcceptanceCriterion("receipt", "host.receipt", "1"),
                ),
            ),
        ),
        execution_envelope=ExecutionEnvelope(
            profile_ref=VersionedRef("profile/reviewer", "A"),
            workspace_ref=VersionedRef("workspace/demo", workspace_version),
            policy_ref=VersionedRef("policy/read-only", "v1"),
            deployment_ref=VersionedRef("deployment/neutral-host", "v1"),
            budget_ref=VersionedRef("budget/offline", "v1"),
        ),
        created_by=PRINCIPAL,
        created_at=time.time(),
    )


def _harness_for(
    control: SqliteControlStore,
    backend: SqliteExecutionBackend,
    workspace: Path,
    provider_log: Path,
    handler_log: Path,
    task: Any,
    attempt_id: str,
    *,
    suspend_after_read: bool = False,
) -> DefaultHarness:
    definition = _read_tool(workspace, handler_log)
    context = RunContext(
        identity=RunIdentity(
            namespace=task.namespace,
            session_id=task.session_id,
            run_id=task.run_id,
            attempt_id=attempt_id,
        ),
        principal=Principal(subject=PRINCIPAL),
    )
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=backend,
    )
    gateway_bindings = GatewayBindings(gateway=gateway, context=context)
    return DefaultHarness(
        stream_fn=CoreStreamBridge(ReadThenAnswerProvider(provider_log)),
        tools=[core_tool(definition, gateway_bindings)],
        persistence=control,
        control=control,
        task_id=task.task_id,
        system_prompt="Use only the approved read tool to inspect the selected source.",
        fingerprints={
            "policy": "read-policy-v1",
            "deployment": "neutral-host-reference-v1",
        },
        suspend_after_batch=(
            (lambda _message, _results: {
                "kind": "host_recovery_check",
                "detail": "pause after the durable read so the host can verify recovery config",
            })
            if suspend_after_read
            else None
        ),
        lease_ttl_seconds=30.0,
    )


def _bound_worker(
    control: SqliteControlStore,
    backend: SqliteExecutionBackend,
    workspace: Path,
    provider_log: Path,
    handler_log: Path,
    *,
    profile: str,
    holder: str,
    resolver: Any | None,
    suspend_after_read: bool = False,
    clock=time.time,
) -> LocalWorker:
    return LocalWorker(
        control,
        lambda task, attempt_id: _harness_for(
            control,
            backend,
            workspace,
            provider_log,
            handler_log,
            task,
            attempt_id,
            suspend_after_read=suspend_after_read,
        ),
        limits=SchedulerLimits(claim_ttl_seconds=0.1),
        clock=clock,
        holder=holder,
        principal=PRINCIPAL,
        enable_orchestration=True,
        orchestration_namespace=NAMESPACE,
        execution_binding_resolver=resolver,
        execution_binding_guard=AllowBoundDeployment(),
        execution_deployment_for=lambda _harness: _descriptor(workspace, profile),
    )


async def _child(
    control_path: Path,
    operations_path: Path,
    workspace: Path,
    provider_log: Path,
    handler_log: Path,
) -> None:
    control = SqliteControlStore(control_path, enable_orchestration=True)
    backend = SqliteExecutionBackend(operations_path)
    try:
        worker = _bound_worker(
            control,
            backend,
            workspace,
            provider_log,
            handler_log,
            profile="A",
            holder="profile-a-child",
            resolver=StaticResolver(workspace, "A"),
            suspend_after_read=True,
        )
        result = await worker.run_once()
        if result is None or result.outcome != "waiting":
            raise RuntimeError(f"Plan Task did not suspend after its read: {result!r}")
        bindings = await control.read_plan_bindings(NAMESPACE, PLAN_ID, 1)
        binding = next(item for item in bindings if item.step_id == "inspect")
        task = await control.read_task(binding.task_id)
        run = await control.load_run(binding.run_id)
        if task is None or run is None:
            raise RuntimeError("the SQLite control store lost the admitted Task or Run")
        receipts = [
            item
            for item in backend.all_operations()
            if item["run_id"] == binding.run_id and item["state"] == "succeeded"
        ]
        if task.status.value != "waiting" or run.state.get("status") != "suspended":
            raise RuntimeError("the child did not commit the expected suspended recovery boundary")
        if len(receipts) != 1:
            raise RuntimeError("the real Gateway did not persist exactly one read receipt")
        print(
            json.dumps(
                {
                    "entry": "plan_task",
                    "stage": "child_exit",
                    "binding_id": binding.binding_id,
                    "task_id": binding.task_id,
                    "run_id": binding.run_id,
                    "task_status": task.status.value,
                    "run_status": run.state["status"],
                    "profile_fingerprint": _descriptor(workspace, "A").profile_fingerprint,
                    "workspace_version": _descriptor(workspace, "A").workspace_version,
                    "gateway_receipts": len(receipts),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        # The parent must reopen both real SQLite stores after this abrupt process exit.
        os._exit(9)
    finally:
        backend.close()
        control.close()


async def _legacy_entries(control: SqliteControlStore) -> None:
    independent = DefaultHarness(
        stream_fn=CoreStreamBridge(CompleteProvider()),
        persistence=control,
        system_prompt="Complete the request.",
    )
    independent_context = RunContext(
        identity=RunIdentity(
            namespace=NAMESPACE,
            session_id=SESSION_ID,
            run_id="legacy-independent-run",
            attempt_id="legacy-independent-a1",
        ),
        principal=Principal(subject=PRINCIPAL),
    )
    independent_outcome = await independent.start(
        RunRequest(prompt="Run independently through the existing Harness API."),
        independent_context,
    )
    if independent_outcome.status.value != "completed":
        raise RuntimeError("the independent legacy Run did not complete")
    print(
        json.dumps(
            {
                "entry": "independent_run",
                "status": independent_outcome.status.value,
                "execution_binding_adapter": False,
            },
            ensure_ascii=False,
        )
    )

    worker = LocalWorker(
        control,
        lambda task, _attempt_id: DefaultHarness(
            stream_fn=CoreStreamBridge(CompleteProvider()),
            persistence=control,
            control=control,
            task_id=task.task_id,
            system_prompt="Complete the ordinary task.",
        ),
        holder="ordinary-task-worker",
        principal=PRINCIPAL,
    )
    ordinary_task = await worker.submit(
        TaskAdmission(
            namespace=NAMESPACE,
            session_id=SESSION_ID,
            task_id="legacy-ordinary-task",
            run_id="legacy-ordinary-run",
            initial_input=InputContent(
                kind=InputKind.INITIAL,
                text="Run an ordinary Task without Plan execution-binding adapters.",
            ),
            created_at=time.time(),
        )
    )
    result = await worker.run_once()
    settled = await control.read_task(ordinary_task.task_id)
    if result is None or settled is None or settled.status.value != "completed":
        raise RuntimeError(f"the ordinary Task entry did not complete: {result!r}")
    print(
        json.dumps(
            {
                "entry": "ordinary_task",
                "status": settled.status.value,
                "execution_binding_adapter": False,
            },
            ensure_ascii=False,
        )
    )


async def _parent(root: Path) -> None:
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text(SOURCE_V1, encoding="utf-8")
    control_path = root / "control.sqlite"
    operations_path = root / "operations.sqlite"
    provider_log = root / "provider.log"
    handler_log = root / "handler.log"

    control = SqliteControlStore(control_path, enable_orchestration=True)
    try:
        await _legacy_entries(control)
        definition = _plan_definition(workspace)
        approved = await approve_plan(control, definition)
        print(
            json.dumps(
                {
                    "entry": "plan_task",
                    "stage": "approved",
                    "plan_revision": approved.record.current_revision,
                    "profile_ref": definition.execution_envelope.profile_ref.to_record(),
                    "workspace_ref": definition.execution_envelope.workspace_ref.to_record(),
                    "tool": "notes.read",
                    "effect": "read",
                    "capabilities": list(_descriptor(workspace, "A").capabilities),
                    "workspace_isolation": "unsupported",
                    "workspace_isolation_claimed": False,
                },
                ensure_ascii=False,
            )
        )
    finally:
        control.close()

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        str(control_path),
        str(operations_path),
        str(workspace),
        str(provider_log),
        str(handler_log),
    ]
    child_result = subprocess.run(command, capture_output=True, text=True, check=False)
    if child_result.returncode != 9:
        raise RuntimeError(
            f"execution-binding child exited {child_result.returncode}, expected 9\n"
            f"stdout:\n{child_result.stdout}\nstderr:\n{child_result.stderr}"
        )
    print(child_result.stdout.strip())

    control = SqliteControlStore(control_path, enable_orchestration=True)
    backend = SqliteExecutionBackend(operations_path)
    try:
        bindings = await control.read_plan_bindings(NAMESPACE, PLAN_ID, 1)
        plan_binding = next(item for item in bindings if item.step_id == "inspect")
        saved = await control.read_plan_execution_binding_for_task(plan_binding.task_id)
        task = await control.read_task(plan_binding.task_id)
        run = await control.load_run(plan_binding.run_id)
        if saved is None or task is None or run is None:
            raise RuntimeError("restart could not read the committed binding, Task and Run")
        if task.status.value != "waiting" or run.state.get("status") != "suspended":
            raise RuntimeError("restart did not find the durable waiting Task and suspended Run")
        receipt_count = len(
            [
                item
                for item in backend.all_operations()
                if item["run_id"] == plan_binding.run_id and item["state"] == "succeeded"
            ]
        )
        if receipt_count != 1:
            raise RuntimeError("the read receipt did not survive process exit")

        provider_before = _line_count(provider_log)
        handler_before = _line_count(handler_log)
        (workspace / "source.txt").write_text(SOURCE_V2, encoding="utf-8")
        changed_dispatch = ExecutionBindingDispatch(
            saved,
            AllowBoundDeployment(),
            lambda: _descriptor(workspace, "B"),
        )
        try:
            await changed_dispatch.check_model_dispatch()
        except ExecutionBindingRefused as refusal:
            if refusal.reason_code != "actual_deployment_mismatch":
                raise
            print(
                json.dumps(
                    {
                        "entry": "plan_task",
                        "stage": "drift_refused",
                        "reason_code": refusal.reason_code,
                        "detail": refusal.detail,
                        "new_model_dispatches": _line_count(provider_log) - provider_before,
                        "new_handler_calls": _line_count(handler_log) - handler_before,
                        "gateway_receipts": receipt_count,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            raise RuntimeError("changed profile/workspace unexpectedly passed the saved binding")
        if _line_count(provider_log) != provider_before or _line_count(handler_log) != handler_before:
            raise RuntimeError("deployment drift reached a provider or actual Gateway handler")

        # The host restored the exact approved profile and workspace snapshot. Wake is
        # authorized only after the host records that recovery fact in the Task inbox.
        (workspace / "source.txt").write_text(SOURCE_V1, encoding="utf-8")
        recovery_at = time.time() + 1.0
        worker = _bound_worker(
            control,
            backend,
            workspace,
            provider_log,
            handler_log,
            profile="A",
            holder="profile-a-recovery",
            resolver=None,
            clock=lambda: recovery_at,
        )
        await worker.submit_input(
            InputAdmission(
                namespace=NAMESPACE,
                session_id=SESSION_ID,
                task_id=plan_binding.task_id,
                input_id="host-restored-approved-deployment",
                content=InputContent(
                    kind=InputKind.FOLLOW_UP,
                    text="The host restored profile A and the approved workspace snapshot.",
                ),
            )
        )
        await worker.wake(plan_binding.task_id)
        recovered = await worker.run_once(now=recovery_at)
        settled = await control.read_task(plan_binding.task_id)
        final_run = await control.load_run(plan_binding.run_id)
        if (
            recovered is None
            or settled is None
            or settled.status.value != "completed"
            or final_run is None
            or final_run.state.get("status") != "completed"
        ):
            raise RuntimeError(f"the restored deployment did not recover the same Task/Run: {recovered!r}")
        final_receipt_count = len(
            [
                item
                for item in backend.all_operations()
                if item["run_id"] == plan_binding.run_id and item["state"] == "succeeded"
            ]
        )
        if final_receipt_count != 1 or _line_count(handler_log) != handler_before:
            raise RuntimeError("recovery reran the already-receipted Gateway handler")

        coordinator = acceptance_coordinator(control, backend)
        reconciliation = await coordinator.reconcile(
            NAMESPACE, PLAN_ID, principal=PRINCIPAL, now=recovery_at + 0.01
        )
        if reconciliation.accepted != 1:
            raise RuntimeError(f"the recovered receipt was not accepted: {reconciliation!r}")
        await deliver_plan_notices(control, PLAN_ID)
        latest = await control.read_plan(NAMESPACE, PLAN_ID)
        assert latest is not None
        completed = await coordinator.finalize_plan(
            NAMESPACE,
            PLAN_ID,
            expected_control_version=latest.record.control_version,
            request_id="finalize-execution-binding-reference",
            principal=PRINCIPAL,
            now=recovery_at + 0.02,
        )
        print(
            json.dumps(
                {
                    "entry": "plan_task",
                    "stage": "restored_config_recovery",
                    "task_status": settled.status.value,
                    "run_status": final_run.state["status"],
                    "plan_status": completed.status.value,
                    "binding_reused": saved
                    == await control.read_plan_execution_binding_for_task(plan_binding.task_id),
                    "gateway_receipts": final_receipt_count,
                    "provider_dispatches": _line_count(provider_log),
                    "handler_calls": _line_count(handler_log),
                },
                ensure_ascii=False,
            )
        )
    finally:
        backend.close()
        control.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    if args.child:
        if len(args.paths) != 5:
            parser.error("--child requires control_path, operations_path, workspace, provider_log, handler_log")
        control_path, operations_path, workspace, provider_log, handler_log = map(Path, args.paths)
        asyncio.run(_child(control_path, operations_path, workspace, provider_log, handler_log))
        return
    with tempfile.TemporaryDirectory(prefix="yanxu-execution-binding-") as temporary:
        asyncio.run(_parent(Path(temporary)))


if __name__ == "__main__":
    main()
