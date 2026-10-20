"""EXT-AC36: the manual APIs, versions and package surface stay compatible."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from yanxu_harness import __version__
from yanxu_harness.errors import VersionRefused
from yanxu_harness.runtime import (
    RUN_RECORD_SCHEMA_VERSION,
    DefaultHarness,
    RunRecord,
    RunStatus,
)
from yanxu_harness.testing import check_extension_deployment_contract
from yanxu_harness.tools import ToolRegistry
from yanxu_harness.versioning import (
    SUPPORTED_CHECKPOINT_VERSIONS,
    SUPPORTED_RUN_RECORD_VERSIONS,
)

ROOT = Path(__file__).resolve().parents[2]


def test_the_package_version_and_persisted_versions_are_unchanged() -> None:
    assert __version__ == "0.3.0a1"
    assert RUN_RECORD_SCHEMA_VERSION == 1
    assert SUPPORTED_RUN_RECORD_VERSIONS == (1,)
    assert SUPPORTED_CHECKPOINT_VERSIONS == (1,)


def test_an_old_run_record_without_an_execution_snapshot_still_reads() -> None:
    record = RunRecord.from_record(
        {
            "schema_version": 1,
            "run_id": "old-run",
            "namespace": "project-a",
            "session_id": "s1",
            "status": "suspended",
            "attempt_id": "a1",
            "pending_interaction": {"kind": "approval"},
            "messages": [],
        }
    )
    assert record.run_id == "old-run"
    assert record.status is RunStatus.SUSPENDED
    assert record.execution is None


def test_an_unknown_run_record_version_is_still_refused() -> None:
    with pytest.raises(VersionRefused):
        RunRecord.from_record(
            {
                "schema_version": 99,
                "run_id": "future",
                "namespace": "project-a",
                "status": "completed",
            }
        )


def test_the_manual_tool_and_harness_surface_is_still_present() -> None:
    from yanxu_harness.skills.memory import InMemorySkillProvider
    from yanxu_harness.tools import ToolDefinition, ToolGateway, core_tool

    assert all(
        item is not None
        for item in (
            ToolDefinition,
            ToolRegistry,
            ToolGateway,
            core_tool,
            InMemorySkillProvider,
            DefaultHarness,
            check_extension_deployment_contract,
        )
    )


def test_the_public_extension_module_is_importable_and_lazy() -> None:
    import yanxu_harness.extensions as extensions

    assert callable(extensions.parse_manifest)
    assert callable(extensions.plan_extensions)
    assert callable(extensions.open_extensions)


@pytest.mark.parametrize("example", ["01_basic_dialogue.py", "02_approved_data_edit.py"])
def test_manual_examples_still_run(example: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / example)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
