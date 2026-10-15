"""The managed-context example, run the way the gate runs it.

The example is a *trial*, not a print statement: it asserts its own claims and exits
non-zero when one fails, so this test is the ordinary-suite copy of what
`scripts/verify_release.py` runs outside the source tree (`examples/*.py`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from package_paths import PACKAGE_ROOT, source_root

EXAMPLE = "06_managed_context_edit.py"


def _project_root() -> Path:
    local = Path(__file__).resolve().parents[1]
    if (local / "examples").is_dir():
        return local
    return PACKAGE_ROOT


def test_the_managed_context_example_runs_its_whole_chain_offline() -> None:
    root = _project_root()
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = str(source_root()) + (os.pathsep + existing if existing else "")
    completed = subprocess.run(
        [sys.executable, str(root / "examples" / EXAMPLE)],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=child_env,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    stdout = completed.stdout
    # The trial prints the evidence it checked, so a passing run is readable.
    assert "transcript kept" in stdout
    assert "wire views" in stdout
    assert "projection" in stdout and "baseline_synthesized=True" in stdout
    assert "the transcript stayed whole" in stdout
