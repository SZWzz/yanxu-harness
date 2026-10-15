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
#    The requirement list is read from pyproject.toml — the build backend plus the
#    dev/yaml/pydantic extras — so it cannot fall behind the declaration.
python scripts/ci_check.py --prepare-wheelhouse /tmp/ci-wheelhouse

# 2) run the job offline
python scripts/ci_check.py --offline --wheelhouse /tmp/ci-wheelhouse
```

Every constraint is quoted because the shell would otherwise read `>=77.0.3` as a
redirection: `setuptools>=77.0.3` silently becomes the unversioned `setuptools`
plus a stray file named `=77.0.3`. Pass `--print-wheelhouse-command` to get the
command this script would run, already quoted for a POSIX shell.

Each step prints `ok`/`FAILED` with its exit code. Exit status is non-zero if any
step failed, so it can be used as a local pre-push gate.

The run owns one artifacts directory (default `build/ci-check`, override with
`--artifacts-dir`). It is removed again unless `--keep-artifacts` is passed, and
only if *this run* created it: a directory that already existed is never wiped, so
a mistyped path cannot delete someone else's files.

A wheelhouse is **interpreter-specific**: ``pip download --only-binary=:all:`` on
3.12 gives ``cp312`` wheels for anything with an extension module (``pydantic-core``,
``pyyaml``), so reusing that directory for ``--interpreter .../3.10/bin/python``
fails with "no matching distribution". CI avoids this by preparing the wheelhouse
inside each matrix job; locally, build one wheelhouse per interpreter.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The package root: this script lives in `<package>/scripts/`, so the repository
#: is its parent. Nothing here may derive a path from *outside* this directory —
#: an exported snapshot has no parent repository to point at.
PACKAGE = Path(__file__).resolve().parents[1]

#: Where a locally prepared wheelhouse is looked for when `--wheelhouse` is omitted.
DEFAULT_WHEELHOUSE = Path(tempfile.gettempdir()) / "yanxu-ci-wheelhouse"

#: Scratch space this run owns. Under `build/`, which is already ignored.
DEFAULT_ARTIFACTS_DIR = PACKAGE / "build" / "ci-check"


def _log(message: str) -> None:
    print(message, flush=True)


def _verify_release():
    """Load the release verifier so its requirement parsing has one home.

    `scripts/` is a directory of standalone entry points, not an importable
    package; loading by path keeps both scripts runnable on their own while the
    wheelhouse requirement list stays defined once.
    """
    spec = importlib.util.spec_from_file_location(
        "yanxu_verify_release", PACKAGE / "scripts" / "verify_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_artifacts(directory: Path) -> tuple[Path, bool]:
    """Create the run's artifacts directory; report whether this run created it."""
    directory = Path(directory).expanduser()
    created_here = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    return directory, created_here


def cleanup_artifacts(directory: Path, *, created_here: bool, keep: bool) -> str:
    """Remove only the directory this run created. Returns what happened.

    `--keep-artifacts` keeps it; a directory that existed before the run is never
    deleted, because "clean up my own scratch space" must not become "delete the
    directory the user pointed me at".
    """
    if keep:
        return f"artifacts kept in {directory}"
    if not created_here:
        return f"artifacts left in {directory} (it existed before this run)"
    shutil.rmtree(directory, ignore_errors=True)
    return f"artifacts removed from {directory}"


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
            cwd=PACKAGE,
        )
    return _run(
        f"install the package with extras [{extras}]",
        [interpreter, "-m", "pip", "install", "-e", f"{PACKAGE}[{extras}]"],
        cwd=PACKAGE,
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


def step_release_verification(
    interpreter: str, wheelhouse: Path, *, offline: bool, workspace: Path
) -> bool:
    args = [
        interpreter,
        str(PACKAGE / "scripts" / "verify_release.py"),
        "--workspace",
        str(workspace),
        "--keep",
    ]
    if offline:
        args += ["--offline", "--wheelhouse", str(wheelhouse)]
    return _run(
        "build wheel+sdist, install outside the tree, run the examples",
        args,
        cwd=PACKAGE,
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


def prepare_wheelhouse(directory: Path) -> int:
    """The one network step: download exactly what `pyproject.toml` declares."""
    verify_release = _verify_release()
    directory.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, *verify_release.wheelhouse_command(directory)[1:]]
    _log(f"preparing {directory}")
    completed = subprocess.run(
        args,
        cwd=str(directory),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        check=False,
    )
    if completed.returncode != 0:
        return completed.returncode
    missing = verify_release.missing_wheelhouse_requirements(directory)
    if missing:
        _log(f"FAILED: dependency preparation did not produce: {', '.join(missing)}")
        return 1
    _log(f"ok: {len(list(directory.glob('*.whl')))} wheels in {directory}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", default=None, help="directory holding the dependencies")
    parser.add_argument(
        "--offline", action="store_true", help="install and build with no index access at all"
    )
    parser.add_argument("--interpreter", default=sys.executable)
    parser.add_argument("--skip-install", action="store_true", help="assume the package is installed")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument(
        "--artifacts-dir",
        default=str(DEFAULT_ARTIFACTS_DIR),
        help="scratch directory this run owns (default: build/ci-check)",
    )
    parser.add_argument(
        "--prepare-wheelhouse",
        default=None,
        metavar="DIR",
        help="download the declared dependencies into DIR and exit (the only network step)",
    )
    parser.add_argument(
        "--print-wheelhouse-command",
        action="store_true",
        help="print the dependency preparation command, quoted for a POSIX shell, and exit",
    )
    args = parser.parse_args(argv)

    verify_release = _verify_release()
    if args.print_wheelhouse_command:
        print(verify_release.wheelhouse_command_line(args.wheelhouse or DEFAULT_WHEELHOUSE))
        return 0
    if args.prepare_wheelhouse:
        return prepare_wheelhouse(Path(args.prepare_wheelhouse))

    wheelhouse = Path(args.wheelhouse).expanduser().resolve() if args.wheelhouse else DEFAULT_WHEELHOUSE
    if args.offline:
        missing = verify_release.missing_wheelhouse_requirements(wheelhouse)
        if not wheelhouse.is_dir() or missing:
            detail = f"it cannot satisfy: {', '.join(missing)}" if missing else "it does not exist"
            print(
                f"--offline needs a prepared wheelhouse; {wheelhouse} {detail}.\n"
                f"prepare it with: {verify_release.wheelhouse_command_line(wheelhouse)}",
                file=sys.stderr,
            )
            return 2

    artifacts_dir = Path(args.artifacts_dir).expanduser()
    artifacts_dir, created_here = prepare_artifacts(artifacts_dir)
    _log(f"interpreter: {args.interpreter}")
    _log(f"offline: {args.offline}    wheelhouse: {wheelhouse}")
    _log(f"artifacts: {artifacts_dir}")
    results: dict[str, bool] = {}
    if not args.skip_install:
        results["install"] = step_install(args.interpreter, wheelhouse, offline=args.offline)
    results["tests"] = step_package_tests(args.interpreter)
    results["cold_import"] = step_no_unguarded_import(args.interpreter)
    results["release"] = step_release_verification(
        args.interpreter,
        wheelhouse,
        offline=args.offline,
        # A fresh subdirectory per run, so a kept run never collides with the next.
        workspace=artifacts_dir / f"release-{os.getpid()}",
    )
    results["doc_links"] = step_doc_links()
    results["licenses"] = step_license_files()

    failed = sorted(name for name, ok in results.items() if not ok)
    _log("")
    _log(f"steps: {len(results)}    failed: {failed or 'none'}")
    _log(cleanup_artifacts(artifacts_dir, created_here=created_here, keep=args.keep_artifacts))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
