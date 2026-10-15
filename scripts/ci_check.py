"""Run the same checks the independent CI job runs, locally and offline.

Why this exists: a workflow file that has never been executed is a *plan*, not a
gate. Everything the `harness` workflow does is ordinary commands, so it can also
be run on a workstation — and then "CI would catch this" is a claim with a command
behind it instead of a hope.

The workflow stays thin and calls this script, so there is exactly one definition
of what the job checks (a second copy in YAML would drift the first time someone
edits one of them).

```bash
# 1) prepare dependencies (the only step that may use the network).
#    The list is exactly the package's dev/yaml/pydantic extras plus the build
#    backend: offline mode resolves *everything* from here, including the
#    backend pip uses for its isolated build.
python -m pip download --only-binary=:all: setuptools>=77 wheel pytest>=7 \
    pytest-asyncio>=0.21 build>=1.0 pyyaml>=6.0 pydantic>=2.0 -d /tmp/ci-wheelhouse

# 2) run the job offline
python scripts/ci_check.py --offline --wheelhouse /tmp/ci-wheelhouse
```

Each step prints `ok`/`FAILED` with its exit code. Exit status is non-zero if any
step failed, so it can be used as a local pre-push gate.

A wheelhouse is **interpreter-specific**: ``pip download --only-binary=:all:`` on
3.12 gives ``cp312`` wheels for anything with an extension module (``pydantic-core``,
``pyyaml``), so reusing that directory for ``--interpreter .../3.10/bin/python``
fails with "no matching distribution". CI avoids this by preparing the wheelhouse
inside each matrix job; locally, build one wheelhouse per interpreter.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[1]


def _log(message: str) -> None:
    print(message, flush=True)


def _run(label: str, args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> bool:
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    if env:
        child_env.update(env)
    completed = subprocess.run(
        args, cwd=str(cwd), env=child_env, capture_output=True, text=True, encoding="utf-8", check=False
    )
    status = "ok" if completed.returncode == 0 else f"FAILED (exit {completed.returncode})"
    _log(f"[{status}] {label}")
    if completed.returncode != 0:
        tail = (completed.stdout + completed.stderr).strip().splitlines()
        for line in tail[-15:]:
            _log(f"    {line}")
    else:
        summary = [line for line in completed.stdout.splitlines() if line.strip()]
        if summary:
            _log(f"    {summary[-1]}")
    return completed.returncode == 0


def _offline_env(wheelhouse: Path, *, offline: bool) -> dict[str, str]:
    if not offline:
        return {}
    return {
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
    }


def step_install(interpreter: str, wheelhouse: Path, *, offline: bool) -> bool:
    extras = "dev,yaml,pydantic"
    if offline:
        # Note: no `--no-build-isolation`. pip builds the editable install in an
        # isolated environment, and in offline mode that environment's backend
        # comes from the wheelhouse — which is why the wheelhouse must contain
        # setuptools and wheel, not just the runtime dependencies.
        return _run(
            f"install the package with extras [{extras}] from the wheelhouse only",
            [interpreter, "-m", "pip", "install", "--no-index", "--find-links", str(wheelhouse),
             "-e", f"{PACKAGE}[{extras}]"],
            cwd=REPO,
        )
    return _run(
        f"install the package with extras [{extras}]",
        [interpreter, "-m", "pip", "install", "-e", f"{PACKAGE}[{extras}]"],
        cwd=REPO,
    )


def step_package_tests(interpreter: str) -> bool:
    return _run(
        "package tests",
        [interpreter, "-m", "pytest", "-q", "tests"],
        cwd=PACKAGE,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )


def step_no_unguarded_import(interpreter: str) -> bool:
    """Import the package in a cold process and assert nothing forbidden came with it."""
    program = "\n".join(
        [
            "import importlib, sys",
            "for name in ('yanxu_harness', 'yanxu_harness.core', 'yanxu_harness.tools',",
            "             'yanxu_harness.storage', 'yanxu_harness.runtime', 'yanxu_harness.providers'):",
            "    importlib.import_module(name)",
            "banned = {'fastapi', 'starlette', 'uvicorn', 'backend', 'storyide', 'pytest'}",
            "leaked = sorted({m.split('.')[0] for m in sys.modules if m.split('.')[0] in banned})",
            "assert leaked == [], leaked",
            "print('base import pulls in nothing it should not')",
        ]
    )
    return _run("cold import pulls in nothing forbidden", [interpreter, "-c", program], cwd=PACKAGE)


def step_release_verification(interpreter: str, wheelhouse: Path, *, offline: bool) -> bool:
    args = [interpreter, str(PACKAGE / "scripts" / "verify_release.py")]
    if offline:
        args += ["--offline", "--wheelhouse", str(wheelhouse)]
    return _run(
        "build wheel+sdist, install outside the tree, run the examples",
        args,
        cwd=REPO,
        env=_offline_env(wheelhouse, offline=offline),
    )


def step_doc_links() -> bool:
    """Every relative markdown target inside the package must resolve."""
    missing: list[str] = []
    for path in sorted(PACKAGE.rglob("*.md")):
        if "node_modules" in path.parts:
            continue
        for target in re.findall(r"\]\(([^)#\s]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).resolve().exists():
                missing.append(f"{path.relative_to(PACKAGE)}: {target}")
    _log(f"[{'ok' if not missing else 'FAILED'}] relative markdown targets resolve")
    for item in missing[:15]:
        _log(f"    {item}")
    return not missing


def step_license_files() -> bool:
    required = ("LICENSE", "THIRD_PARTY_NOTICES.md", "LICENSES/pi-agent-core-MIT.txt")
    missing = [name for name in required if not (PACKAGE / name).is_file()]
    _log(f"[{'ok' if not missing else 'FAILED'}] provenance files present")
    for item in missing:
        _log(f"    missing {item}")
    return not missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", default=None, help="directory holding the dependencies")
    parser.add_argument(
        "--offline", action="store_true", help="install and build with no index access at all"
    )
    parser.add_argument("--interpreter", default=sys.executable)
    parser.add_argument("--skip-install", action="store_true", help="assume the package is installed")
    parser.add_argument("--keep-artifacts", action="store_true")
    args = parser.parse_args(argv)

    wheelhouse = Path(args.wheelhouse).resolve() if args.wheelhouse else Path("/tmp") / "yanxu-ci-wheelhouse"
    if args.offline and not wheelhouse.is_dir():
        print(
            f"--offline needs a prepared wheelhouse; {wheelhouse} does not exist.\n"
            "prepare it with: python -m pip download --only-binary=:all: "
            "setuptools>=77 wheel pytest>=7 pytest-asyncio>=0.21 build>=1.0 pyyaml>=6.0 pydantic>=2.0 "
            f"-d {wheelhouse}",
            file=sys.stderr,
        )
        return 2

    _log(f"interpreter: {args.interpreter}")
    _log(f"offline: {args.offline}    wheelhouse: {wheelhouse}")
    results: dict[str, bool] = {}
    if not args.skip_install:
        results["install"] = step_install(args.interpreter, wheelhouse, offline=args.offline)
    results["tests"] = step_package_tests(args.interpreter)
    results["cold_import"] = step_no_unguarded_import(args.interpreter)
    results["release"] = step_release_verification(
        args.interpreter, wheelhouse, offline=args.offline
    )
    results["doc_links"] = step_doc_links()
    results["licenses"] = step_license_files()

    failed = sorted(name for name, ok in results.items() if not ok)
    _log("")
    _log(f"steps: {len(results)}    failed: {failed or 'none'}")
    if not args.keep_artifacts:
        shutil.rmtree(REPO / "build" / "harness-wheelhouse", ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
