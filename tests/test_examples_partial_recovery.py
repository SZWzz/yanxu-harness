"""The partial-batch recovery example, run the way a developer would run it.

The example crashes a real child process inside a tool call and then recovers the batch
call by call in this process. Running it proves the chain; the assertions on its output
prove that the chain is the one the example claims (a barrier before the evidence, and no
call running twice).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from package_paths import PACKAGE_ROOT, source_root


def _project_root() -> Path:
    local = Path(__file__).resolve().parents[1]
    if (local / "examples").is_dir():
        return local
    return PACKAGE_ROOT


ROOT = _project_root()
EXAMPLE = ROOT / "examples" / "07_partial_batch_recovery.py"


def _child(args: list[str], *, timeout: float = 300.0):
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = str(source_root()) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=child_env,
        check=False,
    )


def test_the_partial_batch_example_recovers_each_call_and_exits_clean() -> None:
    completed = _child([str(EXAMPLE)])
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    assert "OK: a partially settled batch was recovered call by call" in completed.stdout
    for step in ("crash", "recover-barrier", "evidence", "finish"):
        assert f"step: {step}" in completed.stdout, completed.stdout
    assert "B is unproven, so A was not re-run and C was not started" in completed.stdout
    assert "A replayed, B proven by evidence, C run exactly once" in completed.stdout


def test_the_partial_batch_example_leaves_no_ledger_behind() -> None:
    completed = _child([str(EXAMPLE)])
    assert completed.returncode == 0
    directories = {
        line.split(": ", 1)[1]
        for line in completed.stdout.splitlines()
        if line.startswith("state directory: ")
    }
    assert directories, completed.stdout
    assert not any(Path(directory).exists() for directory in directories), (
        "the example must not leave its temporary ledgers behind"
    )
