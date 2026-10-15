"""Spawn a real child process for the fault-window tests.

The harness's durability claims are about *crashes*, so they have to be tested
with processes that actually die — an exception caught inside one interpreter
proves nothing about what a restart can see. Two conventions here exist to keep
the measurement honest:

* **Encoding is declared, never inherited.** The child gets
  ``PYTHONIOENCODING=utf-8`` and the parent decodes with ``encoding="utf-8"``.
  Deliberately *not* ``PYTHONUTF8``: that would also change the child's
  filesystem encoding and so change the behaviour under test.
* **The child's import path is explicit.** ``PYTHONPATH`` is set to wherever the
  package's own source lives (the checkout's ``src``, or site-packages when the
  tests were copied out of the tree), so a child never depends on an editable
  install having been performed on this machine.

This module is self-contained on purpose: the package's tests must not import the
host repository's test helpers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

from package_paths import source_root

TESTS_DIR = Path(__file__).resolve().parent
#: Works in a checkout (`<pkg>/src`) and from an installed wheel
#: (site-packages), so the fault-window children prove the same thing either way.
SRC_DIR = source_root()
CHILD = TESTS_DIR / "fault_windows.py"


def run_child(
    args: Sequence[str],
    *,
    timeout: float = 60.0,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the fault-window child script and return its completed process."""
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, str(CHILD), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=child_env,
        check=False,
    )


def child_json(result: subprocess.CompletedProcess[str]) -> dict:
    """Parse the child's last stdout line as JSON, or fail loudly with its stderr."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(
            f"child produced no output (exit {result.returncode})\nstderr:\n{result.stderr[-2000:]}"
        )
    import json

    try:
        return json.loads(lines[-1])
    except ValueError as exc:
        raise AssertionError(
            f"child's last line is not JSON: {lines[-1]!r}\nstderr:\n{result.stderr[-2000:]}"
        ) from exc
