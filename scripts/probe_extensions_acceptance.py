#!/usr/bin/env python3
"""Independent acceptance probe for the extension layer (EXT-AC34/35).

    python scripts/probe_extensions_acceptance.py --report /tmp/extensions-acceptance.json

The probe does not import pytest. It drives the public API, a real SQLite
harness, the reference pack and a real second process. It reports what it
observed; a failure is a non-zero exit, not a skipped check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parents[1]
for extra in (
    PACKAGE,
    PACKAGE / "src",
    PACKAGE / "tests" / "extensions",
    PACKAGE / "examples" / "extensions",
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from ext_e2_deployment import (  # noqa: E402
    ScriptedStream,
    build,
    final_message,
    tool_call_message,
)
from ext_support import RecordingFactory, binding, manifest, read_tool  # noqa: E402
from reference_pack import LOOKUP_TOOL, build_reference_plan  # noqa: E402

from yanxu_harness.errors import ResumeRefused  # noqa: E402
from yanxu_harness.extensions import (  # noqa: E402
    ExtensionContribution,
    ExtensionOpenFailed,
    ExtensionRefused,
    ExtensionState,
    describe_plan,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.runtime import (  # noqa: E402
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)
from yanxu_harness.storage import SqliteApprovalStore  # noqa: E402
from yanxu_harness.testing import RecordingExecutionBackend  # noqa: E402
from yanxu_harness.tools import (  # noqa: E402
    ApprovalStatus,
    ConservativePolicy,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolGateway,
    ToolInvocation,
)

RESUME_CHILD = r"""
import asyncio, json, sys
from pathlib import Path
from ext_e2_deployment import ScriptedStream, final_message, build, RUN_ID
from yanxu_harness.tools import ApprovalStatus

async def main():
    root = Path(sys.argv[1]); key = sys.argv[2]
    bundle = await build(root, version="1.0.0", attempt="a2", stream=ScriptedStream(final_message("done")))
    await bundle.approvals.decide(key, ApprovalStatus.APPROVED, "probe-child")
    outcome = await bundle.harness.resume(RUN_ID, {}, bundle.context)
    print(json.dumps({
        "status": outcome.status.value,
        "fingerprint": bundle.deployment.fingerprint,
        "effects": bundle.effect_lines(),
        "model_calls": bundle.stream.calls,
    }))
    await bundle.deployment.aclose(); bundle.close()

asyncio.run(main())
"""


def _context() -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"
        ),
        principal=Principal(subject="probe"),
        labels={"authorization_scope": "authorized"},
    )


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PACKAGE / "src"), str(PACKAGE / "tests" / "extensions")]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _two_extension_plan(first_tool: str, second_tool: str, first, second):
    return plan_extensions(
        manifests=[
            manifest(
                extension_id="probe.one",
                tools=(first_tool,),
                skills=(),
                capabilities=("tools.v1",),
            ),
            manifest(
                extension_id="probe.two",
                tools=(second_tool,),
                skills=(),
                capabilities=("tools.v1",),
            ),
        ],
        factories={"probe.one": first, "probe.two": second},
        bindings=[
            binding(
                extension_id="probe.one",
                tools=(first_tool,),
                skills=(),
                capabilities=("tools.v1",),
            ),
            binding(
                extension_id="probe.two",
                tools=(second_tool,),
                skills=(),
                capabilities=("tools.v1",),
            ),
        ],
    )


async def probe_conflict_and_rollback() -> dict[str, Any]:
    first = RecordingFactory(
        ExtensionContribution(tools=(read_tool("probe.one.read"),))
    )
    second = RecordingFactory(
        ExtensionContribution(tools=(read_tool("probe.two.read"),)),
        fail_on_open=True,
    )
    plan = _two_extension_plan("probe.one.read", "probe.two.read", first, second)
    try:
        await open_extensions(plan).open()
    except ExtensionOpenFailed as exc:
        return {
            "ok": first.closed == 1 and second.closed == 1
            and first.resources == 0 and second.resources == 0,
            "error": exc.code,
            "first_closed": first.closed,
            "second_closed": second.closed,
        }
    return {"ok": False, "error": "the failing batch was published"}


async def probe_revocation() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ext-probe-revoke-") as directory:
        plan = build_reference_plan(workspace=Path(directory) / "workspace")
        backend = RecordingExecutionBackend()
        async with open_extensions(plan) as deployment:
            gateway = ToolGateway(
                registry=deployment.registry,
                policy=ConservativePolicy(auto_write=True),
                approvals=InMemoryApprovalStore(),
                backend=backend,
            )
            deployment.invalidate("probe revocation")
            result = await gateway.invoke(
                ToolInvocation(
                    call_id="probe-call",
                    namespace="project-a",
                    tool_name=LOOKUP_TOOL,
                    arguments={"term": "alpha"},
                    run_id="r1",
                ),
                _context(),
            )
            return {
                "ok": result.status is InvocationStatus.DENIED
                and backend.intents == []
                and deployment.state is ExtensionState.INVALIDATED,
                "status": result.status.value,
                "intents": len(backend.intents),
            }


async def probe_version_drift(root: Path) -> dict[str, Any]:
    calls: list[str] = []
    first = await build(
        root,
        version="1.0.0",
        stream=ScriptedStream(tool_call_message()),
        calls=calls,
    )
    paused = await first.harness.start(RunRequest(prompt="write it"), first.context)
    key = paused.pending_interaction["approval_binding_key"]
    fingerprint = first.deployment.fingerprint
    await first.deployment.aclose()
    first.close()

    drift_stream = ScriptedStream(final_message("must not run"))
    upgraded = await build(
        root, version="2.0.0", attempt="a2", stream=drift_stream, calls=calls
    )
    refused = False
    try:
        await upgraded.harness.resume("r1", {}, upgraded.context)
    except ResumeRefused as exc:
        refused = exc.reason_code == "arrangement_drift"
    finally:
        await upgraded.deployment.aclose()
        upgraded.close()

    approvals = SqliteApprovalStore(root / "approvals.db")
    await approvals.decide(key, ApprovalStatus.APPROVED, "probe")
    approvals.close()
    restored_stream = ScriptedStream(final_message("done"))
    restored = await build(
        root, version="1.0.0", attempt="a3", stream=restored_stream, calls=calls
    )
    outcome = await restored.harness.resume("r1", {}, restored.context)
    same = restored.deployment.fingerprint == fingerprint
    effects = restored.effect_lines()
    await restored.deployment.aclose()
    restored.close()
    return {
        "ok": refused
        and outcome.status.value == "completed"
        and effects == ["a3:reference.write:alpha"]
        and drift_stream.calls == 0
        and same,
        "drift_refused": refused,
        "same_fingerprint": same,
        "effects": effects,
        "model_calls_after_drift": drift_stream.calls,
    }


async def probe_cross_process(root: Path) -> dict[str, Any]:
    first = await build(
        root,
        version="1.0.0",
        stream=ScriptedStream(tool_call_message()),
    )
    paused = await first.harness.start(RunRequest(prompt="write it"), first.context)
    key = paused.pending_interaction["approval_binding_key"]
    fingerprint = first.deployment.fingerprint
    await first.deployment.aclose()
    first.close()

    completed = subprocess.run(
        [sys.executable, "-c", RESUME_CHILD, str(root), key],
        cwd=str(root),
        env=_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": (completed.stderr or completed.stdout)[-1500:],
        }
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    return {
        "ok": payload["status"] == "completed"
        and payload["fingerprint"] == fingerprint
        and payload["effects"] == ["a2:reference.write:alpha"],
        **payload,
    }


async def probe_diagnostics() -> dict[str, Any]:
    secret = "nested-canary-7f4a"
    factory = RecordingFactory(
        contribution=lambda: (_ for _ in ()).throw(AssertionError(secret))
    )
    plan = plan_extensions(
        manifests=[manifest()],
        factories={"example.reference": factory},
        bindings=[binding(resource_refs={"credential": secret, "workspace": secret})],
    )
    described = json.dumps(describe_plan(plan), ensure_ascii=False)
    report = json.dumps(described, ensure_ascii=False)
    return {
        "ok": secret not in described
        and secret not in report
        and "resource_refs" not in described
        and factory.opened == 0,
        "factory_opened": factory.opened,
        "secret_in_description": secret in described,
    }


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ext-probe-") as directory:
        root = Path(directory)
        results = {
            "conflict_and_partial_rollback": await probe_conflict_and_rollback(),
            "revocation": await probe_revocation(),
            "version_drift": await probe_version_drift(root / "drift"),
            "cross_process_recovery": await probe_cross_process(root / "cross"),
            "diagnostics": await probe_diagnostics(),
        }
    ok = all(item.get("ok") for item in results.values())
    report = {"ok": ok, "python": sys.version.split()[0], "results": results}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    report = asyncio.run(main())
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        Path(args.report).write_text(rendered, encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if report["ok"] else 1)
