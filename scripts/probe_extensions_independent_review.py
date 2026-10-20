#!/usr/bin/env python3
"""Independent E0–E3 review probes; exit 1 while a contract is violated.

Run from the checkout with .venv/bin/python, or .venv-mcp/bin/python --with-mcp.
The fixtures provide local tools/servers; admission, approvals, effects and resume
use the real public runtime and SQLite implementations. No external model is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "tests/extensions"),
    str(ROOT / "tests/mcp/support"),
    str(ROOT / "examples/extensions"),
]

from ext_support import RecordingFactory, binding, manifest, read_tool, write_tool
from yanxu_harness.extensions import ExtensionContribution, open_extensions, plan_extensions
from yanxu_harness.runtime import Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    InMemoryApprovalStore,
    ToolGateway,
    ToolInvocation,
)


def context(namespace: str = "project-a", scope: str = "authorized") -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace=namespace, session_id="s", run_id="r", attempt_id="a"),
        principal=Principal(subject="independent-review"),
        labels={"authorization_scope": scope},
    )


def invocation(name: str = "example.reference.write", *, namespace: str = "project-a",
               call_id: str = "c", arguments: dict[str, Any] | None = None) -> ToolInvocation:
    return ToolInvocation(
        tool_name=name, call_id=call_id, namespace=namespace, run_id="r",
        arguments={"term": "alpha"} if arguments is None else arguments,
    )


def plan(factory: Any, *, local: tuple[Any, ...] = ()) -> Any:
    return plan_extensions(
        manifests=[manifest(tools=("example.reference.write",), skills=())],
        factories={"example.reference": factory},
        bindings=[binding(tools=("example.reference.write",), skills=())],
        local_tools=local,
    )


def gateway(deployment: Any, backend: Any, approvals: Any, *, auto_write: bool = False) -> ToolGateway:
    return ToolGateway(
        registry=deployment.registry, policy=ConservativePolicy(auto_write=auto_write),
        approvals=approvals, backend=backend,
    )


class BlockingApprovals(InMemoryApprovalStore):
    def __init__(self) -> None:
        super().__init__()
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, key: str) -> Any:
        if self.block:
            self.entered.set()
            await self.release.wait()
        return await super().get(key)


async def approval_wait_revocation(root: Path) -> dict[str, Any]:
    effects: list[str] = []

    async def handler(ctx: Any, args: Any) -> str:
        effects.append(args["term"])
        return "wrote"

    factory = RecordingFactory(ExtensionContribution(tools=(replace(write_tool(), handler=handler),)))
    deployment = await open_extensions(plan(factory)).open()
    backend = SqliteExecutionBackend(root / "race.db")
    approvals = BlockingApprovals()
    task = None
    try:
        gw = gateway(deployment, backend, approvals)
        pending = await gw.invoke(invocation(), context())
        await approvals.decide(pending.approval.binding.key(), ApprovalStatus.APPROVED, "operator")
        approvals.block = True
        task = asyncio.create_task(gw.invoke(invocation(), context()))
        await asyncio.wait_for(approvals.entered.wait(), timeout=5)
        # No admission has occurred: the call is suspended inside approval lookup.
        await deployment.aclose()
        approvals.release.set()
        result = await asyncio.wait_for(task, timeout=5)
        return {"passed": result.status.value == "denied" and not effects,
                "status": result.status.value, "effects": effects,
                "state": deployment.state.value}
    finally:
        approvals.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await deployment.aclose()
        backend.close()


async def local_tool_revocation(root: Path) -> dict[str, Any]:
    effects: list[str] = []

    async def handler(ctx: Any, args: Any) -> str:
        effects.append(args["term"])
        return "read"

    factory = RecordingFactory(ExtensionContribution(tools=(write_tool(),)))
    deployment = await open_extensions(plan(
        factory, local=(replace(read_tool("host.read"), handler=handler),)
    )).open()
    backend = SqliteExecutionBackend(root / "local.db")
    try:
        gw = gateway(deployment, backend, InMemoryApprovalStore())
        await deployment.aclose()
        result = await gw.invoke(invocation("host.read"), context())
        return {"passed": result.status.value == "denied" and not effects,
                "status": result.status.value, "effects": effects,
                "state": deployment.state.value}
    finally:
        await deployment.aclose()
        backend.close()


async def gateway_scope(root: Path) -> dict[str, Any]:
    effects: list[str] = []

    async def handler(ctx: Any, args: Any) -> str:
        effects.append(ctx.identity.namespace)
        return "wrote"

    factory = RecordingFactory(ExtensionContribution(tools=(replace(write_tool(), handler=handler),)))
    deployment = await open_extensions(plan(factory)).open()
    backend = SqliteExecutionBackend(root / "scope.db")
    statuses = []
    try:
        gw = gateway(deployment, backend, InMemoryApprovalStore(), auto_write=True)
        for ns, scope, call_id in (
            ("project-b", "authorized", "wrong-namespace"),
            ("project-a", "unauthorized-b", "wrong-authorization"),
        ):
            try:
                result = await gw.invoke(invocation(namespace=ns, call_id=call_id), context(ns, scope))
                statuses.append(result.status.value)
            except Exception as exc:
                # Only a documented refusal qualifies, not an arbitrary broken probe.
                if type(exc).__name__ not in {"DeploymentRefused", "ExtensionRefused"}:
                    raise
                statuses.append("denied")
        return {"passed": statuses == ["denied", "denied"] and not effects,
                "statuses": statuses, "effects": effects}
    finally:
        await deployment.aclose()
        backend.close()


class FailingCleanupFactory:
    def __init__(self, *, fail_open: bool) -> None:
        self.resources = 0
        self.fail_open = fail_open

    async def open(self, ctx: Any) -> ExtensionContribution:
        self.resources = 1
        if self.fail_open:
            raise RuntimeError("startup failed after acquisition")
        return ExtensionContribution(tools=(write_tool(),))

    async def close(self) -> None:
        # Synthetic private config marker, never a real credential.
        raise RuntimeError("review-private-token-7f43")


async def failed_startup_cleanup(root: Path) -> dict[str, Any]:
    factory = FailingCleanupFactory(fail_open=True)
    deployment = open_extensions(plan(factory))
    try:
        await deployment.open()
    except Exception as exc:
        error_type = type(exc).__name__
    else:
        raise AssertionError("the failing factory unexpectedly opened")
    before = deployment.close_report
    after = await deployment.aclose()
    return {"passed": not before.ok and not after.ok,
            "exception": error_type, "resources": factory.resources,
            "report_ok": before.ok, "report_state": before.state.value,
            "failed_count": len(before.failed), "unconfirmed_count": len(before.unconfirmed),
            "final_report_ok": after.ok}


async def close_report_redaction(root: Path) -> dict[str, Any]:
    factory = FailingCleanupFactory(fail_open=False)
    deployment = await open_extensions(plan(factory)).open()
    report = await deployment.aclose()
    leaked = "review-private-token-7f43" in str(report)
    return {"passed": not report.ok and not leaked,
            "report_ok": report.ok, "failed_count": len(report.failed),
            "synthetic_secret_in_report": leaked}


async def config_override_resume(root: Path) -> dict[str, Any]:
    from ext_e2_deployment import RUN_ID, ScriptedStream, build, final_message, tool_call_message

    class Model:
        provider = "test"
        api = "test"

        def __init__(self, identifier: str) -> None:
            self.id = identifier

    directory = root / "resume"
    first = await build(directory, model=Model("model-a"),
                        fingerprints={"config": "fixed-host-config"},
                        stream=ScriptedStream(tool_call_message()))
    try:
        paused = await first.harness.start(RunRequest(prompt="write it"), first.context)
        await first.approvals.decide(paused.pending_interaction["approval_binding_key"],
                                     ApprovalStatus.APPROVED, "operator")
        old_config = first.harness.fingerprints()["config"]
    finally:
        await first.deployment.aclose()
        first.close()
    stream = ScriptedStream(final_message())
    second = await build(directory, attempt="a2", model=Model("model-b"),
                         fingerprints={"config": "fixed-host-config"}, stream=stream)
    try:
        try:
            result = await second.harness.resume(RUN_ID, {}, second.context)
            status = result.status.value
        except Exception as exc:
            if type(exc).__name__ != "ResumeRefused":
                raise
            status = "refused"
        effects = second.effect_lines()
        return {"passed": status == "refused" and stream.calls == 0 and not effects,
                "status": status, "model_calls": stream.calls, "effects": effects,
                "same_compared_config": old_config == second.harness.fingerprints()["config"]}
    finally:
        await second.deployment.aclose()
        second.close()


async def mcp_gateway_scope(root: Path) -> dict[str, Any]:
    from harness import counter, mcp_stdio_config
    from mcp_reference_pack import build_mcp_reference_pack

    state = root / "mcp-state"
    state.mkdir()
    pack = build_mcp_reference_pack(config=mcp_stdio_config(
        root / "mcp-facts.jsonl", state, authorization_scope="authorized", config_revision="cfg-1"
    ))
    deployment = await open_extensions(pack.plan).open()
    backend = SqliteExecutionBackend(root / "mcp.db")
    try:
        gw = gateway(deployment, backend, InMemoryApprovalStore(), auto_write=True)
        result = await gw.invoke(invocation(
            pack.bump, namespace="project-b", arguments={"key": "cross-scope"}
        ), context("project-b", "unauthorized-b"))
        effects = counter(state, "cross-scope")
        return {"passed": result.status.value == "denied" and effects == 0,
                "status": result.status.value, "remote_effect_count": effects,
                "deployment_namespace": deployment.namespace, "call_namespace": "project-b"}
    finally:
        await deployment.aclose()
        backend.close()


async def run(with_mcp: bool) -> dict[str, Any]:
    probes = [approval_wait_revocation, local_tool_revocation, gateway_scope,
              failed_startup_cleanup, close_report_redaction, config_override_resume]
    if with_mcp:
        probes.append(mcp_gateway_scope)
    with tempfile.TemporaryDirectory(prefix="extensions-review-") as directory:
        results = {}
        for probe in probes:
            results[probe.__name__] = await probe(Path(directory))
    return {"ok": all(item["passed"] for item in results.values()), "checks": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-mcp", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args.with_mcp))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
