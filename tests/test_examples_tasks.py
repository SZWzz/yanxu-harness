"""The three task-orchestration examples, run the way a developer would run them.

Each example is a real child process against real SQLite; only the model is scripted. Running
them proves the chain, and the assertions on their output prove the chain is the one the example
claims (a redelivered input applied once, a fair worker with a wait, and a parent/child tree
recovered across processes).
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
EXAMPLES = {
    "input": ROOT / "examples" / "08_durable_input_redelivery.py",
    "scheduling": ROOT / "examples" / "09_fair_task_scheduling.py",
    "children": ROOT / "examples" / "10_parent_child_recovery.py",
}


def _child(example: Path, *, timeout: float = 300.0):
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = str(source_root()) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, str(example)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=child_env,
        check=False,
    )


def test_the_redelivery_example_applies_once_and_exits_clean() -> None:
    completed = _child(EXAMPLES["input"])
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    for step in ("admitted", "cross-process-admit", "duplicate", "conflict", "applied-once"):
        assert f"step: {step}" in completed.stdout, completed.stdout
    assert "OK: a redelivered input was applied exactly once across processes" in completed.stdout


def test_the_scheduling_example_is_fair_and_waits_without_blocking() -> None:
    completed = _child(EXAMPLES["scheduling"])
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    for step in ("admitted", "aging", "round-robin", "waiting-releases-slot", "resumed"):
        assert f"step: {step}" in completed.stdout, completed.stdout
    assert "OK: the local worker is bounded, fair, and a wait does not block it" in completed.stdout


def test_the_child_recovery_example_recovers_exactly_once() -> None:
    completed = _child(EXAMPLES["children"])
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    for step in ("spawned", "waiting", "child-process", "notified", "applied-once", "parent-finished"):
        assert f"step: {step}" in completed.stdout, completed.stdout
    assert "OK: the parent/child tree recovered across processes exactly once" in completed.stdout


def test_the_task_examples_leave_no_state_behind() -> None:
    for example in EXAMPLES.values():
        completed = _child(example)
        assert completed.returncode == 0, completed.stderr
        directories = {
            line.split(": ", 1)[1]
            for line in completed.stdout.splitlines()
            if line.startswith("state directory: ")
        }
        assert directories, completed.stdout
        assert not any(Path(directory).exists() for directory in directories), (
            f"{example.name} left its temporary state behind"
        )
