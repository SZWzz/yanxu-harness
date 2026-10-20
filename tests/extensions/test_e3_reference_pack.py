"""EXT-AC32/34: the reference pack is real, runnable and revocable."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from yanxu_harness.extensions import ExtensionState, open_extensions
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.testing import (
    RecordingExecutionBackend,
    check_extension_deployment_contract,
)
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolGateway,
    ToolInvocation,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXAMPLES = ROOT / "examples" / "extensions"
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

from reference_pack import (  # noqa: E402
    LOOKUP_TOOL,
    REFERENCE_SKILL_ID,
    WRITE_TOOL,
    build_reference_plan,
)


def _context() -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"
        ),
        principal=Principal(subject="reference"),
        labels={"authorization_scope": "authorized"},
    )


def _invocation(tool: str, call_id: str, **arguments) -> ToolInvocation:
    return ToolInvocation(
        call_id=call_id,
        namespace="project-a",
        tool_name=tool,
        arguments=arguments,
        run_id="r1",
    )


async def test_the_reference_pack_passes_the_extension_contract(tmp_path: Path) -> None:
    plan = build_reference_plan(workspace=tmp_path / "workspace")
    async with open_extensions(plan) as deployment:
        checks = await check_extension_deployment_contract(
            deployment, scope="project-a", skill_ids=(REFERENCE_SKILL_ID,)
        )
        assert "tool-contracts" in checks
        assert "report-bounded" in checks
        assert f"skill:{REFERENCE_SKILL_ID}" in checks
        metadata = await deployment.skills.list_metadata(scope="project-a")
        assert metadata[0].tools == (LOOKUP_TOOL,)


async def test_a_reference_write_is_previewed_approved_and_then_revoked(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    plan = build_reference_plan(workspace=workspace)
    approvals = InMemoryApprovalStore()
    backend = RecordingExecutionBackend()
    async with open_extensions(plan) as deployment:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=backend,
        )
        invocation = _invocation(WRITE_TOOL, "call-write", term="Alpha")
        waiting = await gateway.invoke(invocation, _context())
        assert waiting.status is InvocationStatus.AWAITING_APPROVAL
        assert waiting.preview == {
            "action": "append",
            "target": "reference-writes.jsonl",
            "term": "alpha",
        }
        assert backend.intents == []

        await approvals.decide(
            waiting.approval.binding.key(), ApprovalStatus.APPROVED, "author"
        )
        executed = await gateway.invoke(invocation, _context())
        assert executed.status is InvocationStatus.EXECUTED
        journal = workspace / "reference-writes.jsonl"
        assert journal.read_text(encoding="utf-8").count("\n") == 1

        deployment.invalidate("the reference pack was revoked")
        after = await gateway.invoke(
            _invocation(LOOKUP_TOOL, "call-after", term="alpha"), _context()
        )
        assert after.status is InvocationStatus.DENIED
        assert len(backend.intents) == 1
        assert deployment.state is ExtensionState.INVALIDATED


def test_example_13_runs_and_assembles_the_pack_without_core_changes() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "13_extension_tools_skills.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "completed"
    assert payload["tools"] == [LOOKUP_TOOL, WRITE_TOOL]
    assert payload["skill"] == REFERENCE_SKILL_ID
    assert payload["model_calls"] == 2
    assert payload["deployment"].startswith("sha256:")
