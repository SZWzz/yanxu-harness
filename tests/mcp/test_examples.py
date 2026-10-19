"""MCP07：两个离线示例真的能按文档运行（无模型、无凭据、无网络出口）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from package_paths import PACKAGE_ROOT, source_root  # noqa: E402

ROOT = PACKAGE_ROOT if (PACKAGE_ROOT / "examples").is_dir() else Path(__file__).resolve().parents[2]
EXAMPLES = {
    "stdio": ROOT / "examples" / "11_mcp_stdio.py",
    "http": ROOT / "examples" / "12_mcp_http_recovery.py",
}


def run_example(path: Path, *, timeout: float = 300.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root())
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
        check=False,
    )


def test_the_stdio_example_runs_and_reports_its_calls(tmp_path) -> None:
    result = run_example(EXAMPLES["stdio"])
    assert result.returncode == 0, result.stderr[-800:]
    assert "transport=stdio protocol=2025-11-25 tools=2" in result.stdout
    assert "call echo status=executed is_error=False content='hello from MCP'" in result.stdout
    assert "call bump status=executed is_error=False content='bumped 1'" in result.stdout
    assert "counter=1" in result.stdout
    assert result.stdout.rstrip().endswith("mcp-example: ok")


def test_the_http_recovery_example_runs_the_unknown_window(tmp_path) -> None:
    result = run_example(EXAMPLES["http"])
    assert result.returncode == 0, result.stderr[-800:]
    assert "transport=streamable_http protocol=2025-11-25 tools=1" in result.stdout
    assert "dispatched call status=unknown business_file=1" in result.stdout
    assert "ledger review=unknown" in result.stdout
    assert "promoted state=succeeded" in result.stdout
    assert "after promotion review=replayed receipt=succeeded" in result.stdout
    assert "ledger rows=1 state=succeeded" in result.stdout
    assert result.stdout.rstrip().endswith("mcp-example-http: ok")


def test_the_examples_leave_no_server_process_behind() -> None:
    for path in EXAMPLES.values():
        run_example(path)
    after = _python_children()
    leaked = {pid for pid, cmd in after.items() if "example_mcp_server" in cmd or "example_http_server" in cmd}
    assert not leaked, f"example servers outlived their examples: {leaked}"


def _python_children() -> dict[int, str]:
    result = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True, check=False)
    children: dict[int, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            children[int(pid_text)] = command
        except ValueError:  # pragma: no cover - malformed ps output
            continue
    return children
