"""Shared, fully offline wiring for the Plan DAG examples.

The model providers in this module return fixed responses. The task and run lifecycle,
Core runner, ToolGateway, SQLite control store, and operation ledger are real.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from yanxu_harness.core.types import TextContent
from yanxu_harness.plans import (
    AcceptanceCriterion,
    AcceptanceResult,
    ApprovalDecision,
    ExecutionEnvelope,
    EvidenceSnapshot,
    PlanApprovalRequest,
    PlanDefinition,
    PlanSource,
    StepDefinition,
    VersionedRef,
    VerificationDecision,
    VerifierRegistry,
    AcceptanceCoordinator,
)
from yanxu_harness.providers import CoreStreamBridge
from yanxu_harness.providers.protocol import (
    Provider,
    ProviderEvent,
    ProviderEventKind,
    ProviderRequest,
)
from yanxu_harness.runtime import DefaultHarness, Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks.scheduler import LocalWorker, SchedulerLimits
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

NAMESPACE = "offline-demo"
SESSION_ID = "session-1"
PLAN_ID = "source-review"
PRINCIPAL = "local-host"
REVIEWER = "local-reviewer"

PLAN_CANDIDATE: dict[str, Any] = {
    "title": "Review one source file",
    "goal": "Read the selected source and independently verify the committed evidence.",
    "steps": [
        {
            "step_id": "inspect",
            "title": "Inspect the source",
            "instruction": "Read source.txt through the declared read tool.",
            "depends_on": [],
        },
        {
            "step_id": "verify",
            "title": "Verify the source evidence",
            "instruction": "Read source.txt again and rely on the tool result.",
            "depends_on": ["inspect"],
        },
    ],
}


def plan_candidate_json() -> str:
    return json.dumps(PLAN_CANDIDATE, ensure_ascii=False, sort_keys=True)


def parse_plan_candidate(
    raw: str,
    *,
    namespace: str = NAMESPACE,
    session_id: str = SESSION_ID,
    plan_id: str = PLAN_ID,
    proposal_run_id: str | None = None,
    now: float | None = None,
) -> PlanDefinition:
    """Parse a small provider proposal, then validate it with the public immutable types."""
    candidate = json.loads(raw)
    if not isinstance(candidate, dict) or set(candidate) != {"title", "goal", "steps"}:
        raise ValueError("candidate must contain exactly title, goal, and steps")
    if not isinstance(candidate["steps"], list):
        raise ValueError("candidate steps must be a list")
    criterion = AcceptanceCriterion("receipt", "host.receipt", "1")
    steps = []
    for row in candidate["steps"]:
        if not isinstance(row, dict) or set(row) != {
            "step_id", "title", "instruction", "depends_on"
        }:
            raise ValueError("each step must contain exactly its declared fields")
        steps.append(
            StepDefinition(
                row["step_id"],
                row["title"],
                row["instruction"],
                (criterion,),
                depends_on=tuple(row["depends_on"]),
            )
        )
    proposal_ref = None
    if proposal_run_id is not None:
        from yanxu_harness.plans import RunReference

        proposal_ref = RunReference(namespace, session_id, proposal_run_id)
    return PlanDefinition(
        namespace=namespace,
        session_id=session_id,
        plan_id=plan_id,
        revision=1,
        title=candidate["title"],
        goal=candidate["goal"],
        steps=tuple(steps),
        execution_envelope=ExecutionEnvelope(
            profile_ref=VersionedRef("profile/offline", "1"),
            workspace_ref=VersionedRef("workspace/temp", "1"),
            policy_ref=VersionedRef("policy/read-only", "1"),
            deployment_ref=VersionedRef("deployment/sqlite-demo", "1"),
            budget_ref=VersionedRef("budget/demo", "1"),
        ),
        created_by=PRINCIPAL,
        created_at=time.time() if now is None else now,
        source=PlanSource.MODEL if proposal_run_id else PlanSource.HOST,
        proposal_run_ref=proposal_ref,
    )


class FixedPlanProvider(Provider):
    """A deterministic planning Provider used to show proposal parsing offline."""

    name = "fixed-offline-plan-provider"

    async def stream(
        self, request: ProviderRequest, cancellation: Any = None
    ) -> AsyncIterator[ProviderEvent]:
        del request, cancellation
        yield ProviderEvent(
            kind=ProviderEventKind.TEXT_DELTA, sequence=0, text=plan_candidate_json()
        )
        yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="stop")


class FixedStepProvider(Provider):
    """A fixed provider that asks the existing Runner to call the real Gateway tool."""

    name = "fixed-offline-step-provider"

    async def stream(
        self, request: ProviderRequest, cancellation: Any = None
    ) -> AsyncIterator[ProviderEvent]:
        del cancellation
        tool_messages = [message for message in request.messages if message.get("role") == "tool"]
        if tool_messages:
            observed = json.dumps(tool_messages[-1].get("content"), ensure_ascii=False)
            yield ProviderEvent(
                kind=ProviderEventKind.TEXT_DELTA,
                sequence=0,
                text=f"已读取并收到工具回执：{observed}",
            )
            yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="stop")
            return
        yield ProviderEvent(
            kind=ProviderEventKind.TOOL_CALL,
            sequence=0,
            tool_call_id="read-source",
            tool_name="notes.read",
            arguments_json=json.dumps({"path": "source.txt"}),
        )
        yield ProviderEvent(
            kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="tool_calls"
        )


def source_tool(workspace: Path) -> ToolDefinition:
    root = workspace.resolve()

    def validate(arguments: Mapping[str, Any]) -> dict[str, str]:
        path = arguments.get("path")
        if path != "source.txt":
            raise ValueError("this demo exposes only source.txt")
        return {"path": path}

    def read_source(_context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        path = (root / arguments["path"]).resolve()
        if path.parent != root:
            raise ValueError("path escaped the demo workspace")
        content = path.read_text(encoding="utf-8")
        return {
            "content": [TextContent(text=content)],
            "details": {
                "path": arguments["path"],
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
        }

    return ToolDefinition(
        "notes.read",
        "read the one source file explicitly supplied to this offline example",
        effects=frozenset({ToolEffect.READ}),
        args_validator=validate,
        handler=read_source,
    )


def make_harness(
    control: Any,
    backend: SqliteExecutionBackend,
    workspace: Path,
    task: Any,
    attempt_id: str,
    *,
    harness_type: type[DefaultHarness] = DefaultHarness,
) -> DefaultHarness:
    definition = source_tool(workspace)
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
    bindings = GatewayBindings(gateway=gateway, context=context)
    return harness_type(
        stream_fn=CoreStreamBridge(FixedStepProvider()),
        tools=[core_tool(definition, bindings)],
        persistence=control,
        control=control,
        task_id=task.task_id,
        system_prompt="Use the declared read tool and report its result.",
        fingerprints={"policy": "offline-read-policy-v1"},
        lease_ttl_seconds=30.0,
    )


def make_worker(
    control: Any,
    backend: SqliteExecutionBackend,
    workspace: Path,
    *,
    holder: str,
    clock=time.time,
    claim_ttl_seconds: float = 60.0,
) -> LocalWorker:
    return LocalWorker(
        control,
        lambda task, attempt_id: make_harness(
            control, backend, workspace, task, attempt_id
        ),
        limits=SchedulerLimits(claim_ttl_seconds=claim_ttl_seconds),
        clock=clock,
        holder=holder,
        principal=PRINCIPAL,
        enable_orchestration=True,
        orchestration_namespace=NAMESPACE,
    )


def acceptance_coordinator(control: Any, backend: SqliteExecutionBackend) -> AcceptanceCoordinator:
    async def read_evidence(binding: Any, task: Any, run: Any) -> EvidenceSnapshot:
        operation_rows = [
            item for item in backend.all_operations()
            if item["namespace"] == binding.namespace and item["run_id"] == binding.run_id
        ]
        receipts = [
            (item["operation_id"], backend.receipt(item["operation_id"]))
            for item in operation_rows
        ]
        unsettled = tuple(
            sorted(item.operation_id for item in backend.unsettled(run_id=binding.run_id))
        )
        resolved = [(operation_id, receipt) for operation_id, receipt in receipts if receipt]
        succeeded = sum(receipt.state.value == "succeeded" for _operation_id, receipt in resolved)
        return EvidenceSnapshot(
            namespace=binding.namespace,
            plan_id=binding.plan_id,
            revision=binding.revision,
            step_id=binding.step_id,
            binding_id=binding.binding_id,
            task_id=task.task_id,
            run_id=run.run_id,
            run_version=run.version,
            evidence_refs=tuple(f"operation-receipt:{operation_id}" for operation_id, _ in resolved),
            payload={
                "receipt_count": len(resolved),
                "succeeded_count": succeeded,
                "all_succeeded": bool(resolved) and succeeded == len(resolved),
            },
            unresolved_operations=unsettled,
            source_versions={"run": str(run.version), "operation_rows": str(len(operation_rows))},
        )

    verifiers = VerifierRegistry()

    async def verify(_criterion: Any, evidence: EvidenceSnapshot) -> VerificationDecision:
        if evidence.unresolved_operations:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "operation_unknown")
        accepted = bool(evidence.payload.get("all_succeeded"))
        return VerificationDecision(
            AcceptanceResult.ACCEPTED if accepted else AcceptanceResult.REJECTED,
            "" if accepted else "receipt_missing_or_failed",
        )

    verifiers.register("host.receipt", "1", verify, timeout_seconds=2.0, max_input_bytes=16_384)
    return AcceptanceCoordinator(control, read_evidence, verifiers, run_store=control)


async def approve_plan(control: Any, definition: PlanDefinition) -> Any:
    now = time.time()
    record = await control.create_plan(
        definition,
        request_id=f"create-{definition.plan_id}",
        principal=PRINCIPAL,
        now=now,
    )
    record = await control.request_plan_review(
        definition.namespace,
        definition.plan_id,
        expected_control_version=record.control_version,
        request_id=f"review-{definition.plan_id}",
        principal=PRINCIPAL,
        now=now + 0.01,
    )
    await control.decide_plan_approval(
        definition.namespace,
        definition.plan_id,
        PlanApprovalRequest(
            decision_id=f"approval-{definition.plan_id}",
            decision=ApprovalDecision.APPROVED,
            approval_policy_version="offline-review-v1",
        ),
        expected_control_version=record.control_version,
        request_id=f"approve-{definition.plan_id}",
        principal=REVIEWER,
        now_ms=int((now + 0.02) * 1000),
    )
    snapshot = await control.read_plan(definition.namespace, definition.plan_id)
    assert snapshot is not None
    return snapshot


async def deliver_plan_notices(control: Any, plan_id: str) -> int:
    entries = await control.read_plan_outbox(
        pending_only=True, limit=100, namespace=NAMESPACE, plan_id=plan_id
    )
    delivered = 0
    for entry in entries:
        delivered += bool(await control.deliver_plan_outbox(entry.outbox_id, at=time.time()))
    return delivered


async def run_proposal(provider: Provider, control: Any, proposal_run_id: str) -> Any:
    """Run one fixed planning Provider through the standard Harness/Core bridge."""
    harness = DefaultHarness(
        stream_fn=CoreStreamBridge(provider),
        persistence=control,
        system_prompt="Return the fixed candidate plan as JSON.",
    )
    context = RunContext(
        identity=RunIdentity(
            namespace=NAMESPACE,
            session_id=SESSION_ID,
            run_id=proposal_run_id,
            attempt_id="proposal-a1",
        ),
        principal=Principal(subject=PRINCIPAL),
    )
    return await harness.start(
        RunRequest(prompt="Propose a two-step offline source review."), context
    )
