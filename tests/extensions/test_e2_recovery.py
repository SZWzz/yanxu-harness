"""EXT-AC24/25/27/29: real extension deployment identity and recovery wiring."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import test_runtime_partial_recovery as partial
from durable_edit import ScriptedStream as ManualStream, final_message as manual_final
from ext_e2_deployment import (
    RUN_ID,
    ScriptedStream,
    TOOL_NAME,
    binding,
    build,
    final_message,
    manifest,
    partial_message,
    tool_call_message,
)
from yanxu_harness.errors import DeploymentRefused, ResumeRefused
from yanxu_harness.extensions import ExtensionState
from yanxu_harness.runtime import DefaultHarness, Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.storage import SqliteApprovalStore
from yanxu_harness.tools import ApprovalStatus


def _child_env() -> dict[str, str]:
    package = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(package / "src"), str(Path(__file__).resolve().parent)]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_child(code: str, root: Path, *args: str, timeout: float = 120.0):
    root.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-c", code, str(root), *args],
        cwd=str(root),
        env=_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


RESUME_CHILD = r"""
import asyncio, json, sys
from pathlib import Path
from ext_e2_deployment import ScriptedStream, final_message, build, RUN_ID
from yanxu_harness.tools import ApprovalStatus

async def main():
    root = Path(sys.argv[1])
    key = sys.argv[2]
    bundle = await build(
        root,
        version="1.0.0",
        attempt="a2",
        stream=ScriptedStream(final_message("done")),
    )
    await bundle.approvals.decide(key, ApprovalStatus.APPROVED, "child-operator")
    outcome = await bundle.harness.resume(RUN_ID, {}, bundle.context)
    payload = {
        "status": outcome.status.value,
        "final_text": outcome.final_text,
        "fingerprint": bundle.deployment.fingerprint,
        "effects": bundle.effect_lines(),
        "model_calls": bundle.stream.calls,
        "import_file": __import__("yanxu_harness").__file__,
    }
    await bundle.deployment.aclose()
    bundle.close()
    print(json.dumps(payload))

asyncio.run(main())
"""


class RefusingBinding:
    fingerprint = "sha256:" + "f" * 64
    summary = {"deployment": fingerprint}

    def check(self, context):
        return "the deployment was revoked"


async def test_an_extension_version_change_is_refused_by_a_real_sqlite_resume(tmp_path: Path) -> None:
    calls: list[str] = []
    first = await build(
        tmp_path / "state",
        version="1.0.0",
        calls=calls,
        stream=ScriptedStream(tool_call_message()),
    )
    paused = await first.harness.start(RunRequest(prompt="write it"), first.context)
    assert paused.status.value == "suspended"
    key = paused.pending_interaction["approval_binding_key"]
    original_fingerprint = first.deployment.fingerprint
    await first.deployment.aclose()
    first.close()

    drift_stream = ScriptedStream(final_message("should not be reached"))
    upgraded = await build(
        tmp_path / "state",
        version="2.0.0",
        attempt="a2",
        calls=calls,
        stream=drift_stream,
    )
    try:
        assert upgraded.deployment.fingerprint != original_fingerprint
        with pytest.raises(ResumeRefused) as refused:
            await upgraded.harness.resume(RUN_ID, {}, upgraded.context)
        assert refused.value.reason_code == "arrangement_drift"
        assert "config" in refused.value.drifted
        assert drift_stream.calls == 0, "a refused upgrade must not ask the model"
        assert calls == [], "a refused upgrade must not run the tool"
        assert upgraded.effect_lines() == []
    finally:
        await upgraded.deployment.aclose()
        upgraded.close()

    approvals = SqliteApprovalStore(tmp_path / "state" / "approvals.db")
    try:
        await approvals.decide(key, ApprovalStatus.APPROVED, "operator")
    finally:
        approvals.close()

    same_stream = ScriptedStream(final_message("done"))
    restored = await build(
        tmp_path / "state",
        version="1.0.0",
        attempt="a3",
        calls=calls,
        stream=same_stream,
    )
    try:
        assert restored.deployment.fingerprint == original_fingerprint
        outcome = await restored.harness.resume(RUN_ID, {}, restored.context)
        assert outcome.status.value == "completed"
        assert outcome.final_text == "done"
        assert calls == ["alpha"]
        assert restored.effect_lines() == ["a3:reference.write:alpha"]
        assert same_stream.calls == 1
    finally:
        await restored.deployment.aclose()
        restored.close()


def test_the_same_deployment_resumes_in_a_new_real_process(tmp_path: Path) -> None:
    async def pause() -> tuple[str, str]:
        bundle = await build(
            tmp_path / "state",
            version="1.0.0",
            stream=ScriptedStream(tool_call_message()),
        )
        outcome = await bundle.harness.start(RunRequest(prompt="write it"), bundle.context)
        assert outcome.status.value == "suspended"
        key = outcome.pending_interaction["approval_binding_key"]
        fingerprint = bundle.deployment.fingerprint
        await bundle.deployment.aclose()
        bundle.close()
        return key, fingerprint

    key, fingerprint = asyncio.run(pause())
    completed = _run_child(RESUME_CHILD, tmp_path / "state", key)
    assert completed.returncode == 0, (completed.returncode, completed.stderr[-3000:])
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["status"] == "completed"
    assert payload["final_text"] == "done"
    assert payload["fingerprint"] == fingerprint, "a new process rebuilt the same logical deployment"
    assert payload["effects"] == ["a2:reference.write:alpha"]
    assert payload["model_calls"] == 1
    assert "site-packages" in payload["import_file"] or "/src/" in payload["import_file"]


async def test_each_recovery_branch_checks_the_deployment_before_dispatch(tmp_path: Path) -> None:
    harness = DefaultHarness(
        stream_fn=ScriptedStream(final_message()),
        deployment_binding=RefusingBinding(),
    )
    context = RunContext(
        identity=RunIdentity(
            namespace="project-a", session_id="s1", run_id="r1", attempt_id="a2"
        ),
        principal=Principal(subject="author"),
    )
    record = __import__("yanxu_harness.runtime", fromlist=["RunRecord"]).RunRecord(
        run_id="r1",
        namespace="project-a",
        session_id="s1",
        status=__import__("yanxu_harness.runtime", fromlist=["RunStatus"]).RunStatus.SUSPENDED,
        attempt_id="a1",
        pending_interaction={"kind": "child_result", "child_task_id": "child"},
    )
    with pytest.raises(DeploymentRefused):
        await harness._resume_child_result(  # noqa: SLF001
            record, None, None, dict(record.pending_interaction or {}), context, "a2"
        )
    with pytest.raises(DeploymentRefused):
        await harness._recover_partial_batch(  # noqa: SLF001
            "r1", record, None, None, context, None
        )


def test_a_running_orphan_checks_the_deployment_before_recovering_a_partial_batch(
    tmp_path: Path,
) -> None:
    paths = partial.crash_in_a_partial_batch(tmp_path / "state")
    harness, _gateway = partial.rebuild(
        paths,
        ManualStream(manual_final("done")),
        attempt="a2",
        deployment_binding=RefusingBinding(),
    )
    record = partial.read_record(paths)
    before = partial.ran_tools(paths)

    with pytest.raises(DeploymentRefused):
        partial.run(harness._recover_orphan(RUN_ID, record, partial.run_context("a2")))  # noqa: SLF001
    assert partial.ran_tools(paths) == before, "a revoked deployment dispatches nothing"
    assert partial.operation_id(partial.C) not in partial.ledger_states(paths)


PARTIAL_CRASH_CHILD = r"""
import asyncio, os, sys
from pathlib import Path
from ext_e2_deployment import ScriptedStream, build, final_message, partial_message
from yanxu_harness.runtime import RunRequest

async def main():
    root = Path(sys.argv[1])
    names = ("reference.a", "reference.b", "reference.c")
    holder = {}
    def b_handler(context, arguments):
        with holder["effect_path"].open("a", encoding="utf-8") as handle:
            handle.write(f"{context.identity.attempt_id}:reference.b:{arguments['term']}\n")
        os._exit(9)
    bundle = await build(
        root,
        version="1.0.0",
        names=names,
        attempt="a1",
        handlers={"reference.b": b_handler},
        auto_write=True,
        stream=ScriptedStream(partial_message(names), final_message("done")),
    )
    holder["effect_path"] = bundle.effect_path
    await bundle.harness.start(RunRequest(prompt="do three"), bundle.context)
    os._exit(12)

asyncio.run(main())
"""


def test_a_b_c_converge_through_a_real_extension_deployment(tmp_path: Path) -> None:
    root = tmp_path / "state"
    completed = _run_child(PARTIAL_CRASH_CHILD, root)
    assert completed.returncode == 9, (completed.returncode, completed.stderr[-3000:])
    names = ("reference.a", "reference.b", "reference.c")

    async def scenario() -> None:
        from ext_e2_deployment import build as build_extension

        first = await build_extension(
            root,
            version="1.0.0",
            names=names,
            attempt="a2",
            stream=ScriptedStream(final_message("done")),
            auto_write=True,
            clock=lambda: time.time() + 400.0,
        )
        try:
            first_outcome = await first.harness.resume(RUN_ID, {}, first.context)
            assert first_outcome.status.value == "suspended"
            interaction = dict(first_outcome.pending_interaction or {})
            assert interaction.get("kind") == "reconciliation"
            lines = first.effect_lines()
            assert [line.split(":")[1] for line in lines] == ["reference.a", "reference.b"]
            assert not any("reference.c" in line for line in lines)
        finally:
            await first.deployment.aclose()
            first.close()

        async def reconciler(call, details):
            return {
                "proven": True,
                "state": "succeeded",
                "detail": "the reference store shows B was written",
                "source": "business store",
            }

        second = await build_extension(
            root,
            version="1.0.0",
            names=names,
            attempt="a3",
            stream=ScriptedStream(final_message("done")),
            reconcile=reconciler,
            auto_write=True,
            clock=lambda: time.time() + 400.0,
        )
        try:
            outcome = await second.harness.resume(RUN_ID, {}, second.context)
            assert outcome.status.value == "completed"
            lines = second.effect_lines()
            ran = [line.split(":")[1] for line in lines]
            assert ran == ["reference.a", "reference.b", "reference.c"], (
                "A and B are never re-run; C runs exactly once after the evidence converges"
            )
            states = {
                str(row["tool_name"]): str(row["state"])
                for row in second.backend.all_operations()
            }
            assert states["reference.a"] == "succeeded"
            assert states["reference.b"] == "succeeded"
            assert states["reference.c"] == "succeeded"
        finally:
            await second.deployment.aclose()
            second.close()

    asyncio.run(scenario())


def test_original_model_prompt_and_host_config_drift_is_still_detected(tmp_path: Path) -> None:
    class Model:
        provider = "test"
        api = "test"

        def __init__(self, identifier: str) -> None:
            self.id = identifier

    async def scenario() -> None:
        from ext_e2_deployment import build as build_extension

        root = tmp_path / "state"
        first = await build_extension(
            root,
            version="1.0.0",
            attempt="a1",
            model=Model("model-a"),
            fingerprints={"host_config": "host-cfg-a"},
            stream=ScriptedStream(tool_call_message()),
        )
        paused = await first.harness.start(RunRequest(prompt="write it"), first.context)
        assert paused.status.value == "suspended"
        await first.deployment.aclose()
        first.close()

        async def refused_drift(**changes) -> tuple[ResumeRefused, any]:
            stream = ScriptedStream(final_message("must not run"))
            bundle = await build_extension(
                root,
                version="1.0.0",
                attempt="a2",
                model=changes.get("model", Model("model-a")),
                system_prompt=changes.get("system_prompt", "be brief"),
                fingerprints=changes.get("fingerprints"),
                stream=stream,
            )
            try:
                with pytest.raises(ResumeRefused) as refused:
                    await bundle.harness.resume(RUN_ID, {}, bundle.context)
                assert stream.calls == 0
                assert bundle.effect_lines() == []
                return refused.value, bundle
            finally:
                await bundle.deployment.aclose()
                bundle.close()

        refused, _ = await refused_drift(system_prompt="different trusted instructions")
        assert refused.reason_code == "arrangement_drift"
        assert "instructions" in refused.drifted

        refused, _ = await refused_drift(model=Model("model-b"))
        assert refused.reason_code == "arrangement_drift"
        assert "config" in refused.drifted

        refused, _ = await refused_drift(fingerprints={"host_config": "host-cfg-b"})
        assert refused.reason_code == "arrangement_drift"
        assert "config" in refused.drifted

        # The same host config as the paused deployment is not drift; the approval is
        # still pending, so this resume asks again instead of running.
        bundle = await build_extension(
            root,
            version="1.0.0",
            attempt="a2",
            model=Model("model-a"),
            fingerprints={"host_config": "host-cfg-a"},
            stream=ScriptedStream(final_message("must not run")),
        )
        try:
            outcome = await bundle.harness.resume(RUN_ID, {}, bundle.context)
            assert outcome.status.value == "suspended"
            assert bundle.effect_lines() == []
        finally:
            await bundle.deployment.aclose()
            bundle.close()

    asyncio.run(scenario())
