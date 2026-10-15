"""The end-to-end example, run the way a developer would run it.

Two things are checked, and they are different claims:

* the **default** example performs its whole chain in real child processes and exits
  non-zero if any assertion inside it fails — so the gate that runs `examples/*.py`
  is running a demonstration, not printing a success line;
* the **optional** real-provider script actually drives the loop through a real HTTP
  provider (a loopback fixture, never a vendor), which is what makes "swap the
  provider" a claim with a test behind it. It also exits with a clear code when no
  key is configured, and makes no request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from package_paths import PACKAGE_ROOT, source_root
from wire_fixture import WireFixture, delta_frame, tool_delta_frame


def _project_root() -> Path:
    """Where the examples live, in a checkout *and* in a copied-out tree.

    `scripts/verify_release.py` copies `tests/` and `examples/` next to each other
    outside the source tree, so `PACKAGE_ROOT` (the installed package's parent) has no
    examples there. The test file's own directory works in both layouts: `<root>/tests`
    in a checkout and `<copy>/tests` in the release check.
    """
    local = Path(__file__).resolve().parents[1]
    if (local / "examples").is_dir():
        return local
    return PACKAGE_ROOT


ROOT = _project_root()
EXAMPLE = ROOT / "examples" / "05_durable_approved_edit.py"
OPTIONAL = ROOT / "examples" / "optional" / "real_model_edit.py"


def _child(args: list[str], *, env: dict[str, str] | None = None, timeout: float = 180.0):
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = str(source_root()) + (os.pathsep + existing if existing else "")
    if env:
        child_env.update(env)
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


def test_the_default_example_runs_its_whole_chain_and_exits_clean() -> None:
    completed = _child([str(EXAMPLE)])
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    assert "OK: an approved edit survived its processes" in completed.stdout
    # The chain really is a chain of processes: each step is named in the output.
    for step in ("start", "status", "approve", "resume"):
        assert step in completed.stdout
    # …and the simulated model says so, so nobody mistakes it for a real one.
    assert "not a real model" in completed.stdout


def test_the_default_example_leaves_no_ledger_behind(tmp_path: Path) -> None:
    """It uses a temporary directory: running it twice cannot collide with itself."""
    first = _child([str(EXAMPLE)])
    second = _child([str(EXAMPLE)])
    assert first.returncode == 0 and second.returncode == 0
    state_dirs = {line.split(": ", 1)[1] for line in first.stdout.splitlines() if line.startswith("state directory: ")}
    other_dirs = {line.split(": ", 1)[1] for line in second.stdout.splitlines() if line.startswith("state directory: ")}
    assert state_dirs and other_dirs and state_dirs != other_dirs
    assert not any(Path(directory).exists() for directory in state_dirs | other_dirs)


def test_the_optional_script_refuses_to_run_without_a_key(tmp_path: Path) -> None:
    completed = _child(
        [
            str(OPTIONAL),
            "--state",
            str(tmp_path / "state"),
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--model",
            "unused",
        ],
        env={"YANXU_EXAMPLE_KEY": ""},
    )
    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert "no request will be made" in completed.stderr
    assert not (tmp_path / "state").exists(), "a run without a key must not create ledgers"


def test_the_optional_script_drives_a_real_provider_over_loopback(tmp_path: Path) -> None:
    """A real HTTP provider, real SSE frames, the real loop — and nothing paid."""
    scripts = [
        [
            tool_delta_frame(0, call_id="call-1", name="notes.edit", arguments='{"text": "更短的欢迎语", "expected_version": 1}'),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [delta_frame("改好了。", finish="stop")],
    ]
    state = tmp_path / "state"
    with WireFixture(scripts=scripts) as fixture:
        completed = _child(
            [
                str(OPTIONAL),
                "--state",
                str(state),
                "--base-url",
                fixture.base_url,
                "--model",
                "example-model",
                "--api-key-env",
                "YANXU_EXAMPLE_KEY",
                "--task",
                "把欢迎语改得更短",
                "--approve",
            ],
            env={"YANXU_EXAMPLE_KEY": "sk-" + "loopback-value"},
        )

    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    steps = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    by_step = {item["step"]: item for item in steps if "step" in item}
    assert by_step["start"]["status"] == "suspended"
    assert by_step["start"]["note"]["version"] == 1, "the pause wrote nothing"
    assert by_step["approve"]["status"] == "approved"
    assert by_step["resume"]["status"] == "completed"
    assert by_step["resume"]["note"] == {"path": "notes/welcome.md", "text": "更短的欢迎语", "version": 2}
    assert by_step["resume"]["attempts"] == 1
    # The provider really served two requests: the tool call and the answer.
    assert len(fixture.requests) == 2
    assert "tools" in json.loads(fixture.requests[0]["body"])
    # The key travelled as a header, never in a body or a printed step.
    assert fixture.requests[0]["headers"]["authorization"] == "Bearer sk-loopback-value"
    assert "sk-loopback-value" not in completed.stdout
