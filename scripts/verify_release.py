"""Build this package and prove it works *outside its own source tree*.

    python scripts/verify_release.py [--keep]

What it does, in order:

1. builds a wheel and an sdist from this package directory, with no network;
2. creates two throwaway virtual environments;
3. installs the wheel into one and the sdist into the other, with ``--no-index``
   so a missing dependency cannot be papered over by downloading something;
4. runs ``python -c "import yanxu_harness"`` in a **temporary working directory**
   with ``PYTHONPATH`` cleared, and fails if the imported file lives in this
   source tree (an editable install or a stray ``sys.path`` entry would otherwise
   make the check pass for the wrong reason);
5. copies the examples and the package's own tests out of the tree and runs them
   there, so nothing can reach back into the checkout;
6. prints the artifact names, their sizes and their digests.

The point of running from a *copy* in a temp directory is that "it works on the
machine that built it" is not the claim. Nothing here needs a provider, a
credential, Node.js or the host repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

#: The directory that is being released. Everything the verifier does is relative
#: to it, so the script works the same from the host repository and from an
#: exported snapshot (where there is no "parent repository" at all).
PACKAGE = Path(__file__).resolve().parents[1]


def log(message: str) -> None:
    print(message, flush=True)


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None, check: bool = True):
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env.pop("PYTHONPATH", None)
    if env:
        child_env.update(env)
    completed = subprocess.run(
        args, cwd=str(cwd), env=child_env, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if check and completed.returncode != 0:
        raise SystemExit(
            f"command failed ({completed.returncode}): {' '.join(args)}\n"
            f"stdout:\n{completed.stdout[-3000:]}\nstderr:\n{completed.stderr[-3000:]}"
        )
    return completed


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_wheelhouse(wheelhouse: Path, *, offline: bool) -> list[str]:
    """Put the *build* dependencies in a local directory, then build offline.

    Dependency preparation and network isolation are two separate steps on
    purpose: in ``--offline`` mode the wheelhouse must already exist, and the
    build itself is always run with ``--no-index``, so a missing build dependency
    shows up as a clear error instead of being quietly downloaded.
    """
    wheelhouse.mkdir(parents=True, exist_ok=True)
    present = sorted(path.name for path in wheelhouse.glob("*.whl"))
    if present:
        return present
    if offline:
        raise SystemExit(
            f"--offline was requested but {wheelhouse} is empty; prepare it first with "
            f"`pip download setuptools wheel -d {wheelhouse}`"
        )
    log(f"preparing the build wheelhouse in {wheelhouse} (this step is the only one that may use the network)")
    run(
        [
            sys.executable, "-m", "pip", "download", "--only-binary=:all:",
            "setuptools>=77", "wheel",
            # The dev extra: needed to run the *copied* test suite in the throwaway
            # venv, which is part of proving the artifact rather than the checkout.
            "pytest>=7", "pytest-asyncio>=0.21",
            # ...and the package's own optional extras, so the release check runs
            # the adapters that depend on them instead of skipping them.
            "pyyaml>=6.0", "pydantic>=2.0",
            "-d", str(wheelhouse),
        ],
        cwd=wheelhouse,
    )
    return sorted(path.name for path in wheelhouse.glob("*.whl"))


def build(
    artifacts: Path,
    sdist_dir: Path,
    wheel_dir: Path,
    wheelhouse: Path,
    *,
    offline: bool,
) -> tuple[Path, Path]:
    build_env = {
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
        "PIP_NO_BUILD_ISOLATION": "",
    }
    run(
        [sys.executable, "-m", "pip", "wheel", str(PACKAGE), "--no-deps", "--no-index",
         "--find-links", str(wheelhouse), "-w", str(wheel_dir)],
        cwd=artifacts,
        env=build_env,
    )
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        raise SystemExit("no wheel was produced")

    # `python -m build` is the normal way to get an sdist, but it is not part of
    # the base install. setuptools can do it directly, which keeps this script
    # runnable with nothing but pip.
    runner = sdist_dir / "make_sdist.py"
    runner.write_text(
        "import sys\n"
        "from setuptools import build_meta\n"
        f"sys.stdout.write(build_meta.build_sdist({str(sdist_dir)!r}))\n",
        encoding="utf-8",
    )
    completed = run(
        [sys.executable, str(runner)],
        cwd=PACKAGE,
        env={"PYTHONPATH": os.pathsep.join(_setuptools_paths(wheelhouse, offline=offline))},
    )
    sdists = sorted(sdist_dir.glob("*.tar.gz"))
    if not sdists:
        raise SystemExit(f"no sdist was produced; stdout was: {completed.stdout[-500:]}")
    return wheels[-1], sdists[-1]


def _setuptools_paths(wheelhouse: Path, *, offline: bool) -> list[str]:
    """Locate a setuptools that can build an sdist, without installing anything.

    Order: the wheelhouse (unpacked if needed), then the interpreter's own
    site-packages. Either way the sdist step never reaches the network.
    """
    unpacked = wheelhouse / "unpacked"
    if not unpacked.is_dir() and any(wheelhouse.glob("setuptools-*.whl")):
        unpacked.mkdir(parents=True, exist_ok=True)
        for wheel in sorted(wheelhouse.glob("setuptools-*.whl")):
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(unpacked)
    if (unpacked / "setuptools" / "build_meta.py").is_file():
        return [str(unpacked)]
    try:
        import setuptools  # noqa: F401

        return []
    except ImportError:
        pass
    raise SystemExit(
        "building an sdist needs setuptools; add it to the wheelhouse with "
        f"`pip download setuptools -d {wheelhouse}` or install the package's dev extra"
    )


def make_venv(path: Path, *, with_setuptools: bool = False) -> Path:
    venv.EnvBuilder(with_pip=True, symlinks=True).create(path)
    interpreter = path / ("Scripts" if os.name == "nt" else "bin") / "python"
    if not with_setuptools:
        run([str(interpreter), "-m", "pip", "uninstall", "-y", "setuptools"], cwd=path, check=False)
    return interpreter


def install(interpreter: Path, artifact: Path, wheelhouse: Path) -> None:
    """Install strictly offline, including the package's declared extras.

    An sdist still needs its build backend, so the wheelhouse is the only place
    pip may look for anything. Extras are installed from the artifact itself so
    the metadata, not this script, decides what an extra contains.
    """
    run(
        [str(interpreter), "-m", "pip", "install", "--no-index", "--find-links", str(wheelhouse),
         "--no-deps", str(artifact)],
        cwd=artifact.parent,
    )
    for extra in _declared_extras(artifact):
        if extra == "dev":
            continue  # dev tools are installed separately below
        run(
            [str(interpreter), "-m", "pip", "install", "--no-index", "--find-links", str(wheelhouse),
             f"{artifact}[{extra}]"],
            cwd=artifact.parent,
        )


def _declared_extras(artifact: Path) -> list[str]:
    """Read the extras the artifact itself declares, so this script cannot drift."""
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            metadata = next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            text = archive.read(metadata).decode("utf-8")
    else:
        with tarfile.open(artifact) as archive:
            member = next(
                item for item in archive.getmembers()
                if item.name.endswith("PKG-INFO") and item.isfile()
            )
            text = archive.extractfile(member).read().decode("utf-8")  # type: ignore[union-attr]
    return [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("Provides-Extra:")]


def assert_installed_outside_the_tree(interpreter: Path, workspace: Path) -> str:
    probe = (
        "import json, pathlib, yanxu_harness, yanxu_harness.core.agent_loop as loop;"
        "print(json.dumps({'file': yanxu_harness.__file__, 'version': yanxu_harness.__version__,"
        " 'loop': loop.__file__}))"
    )
    completed = run([str(interpreter), "-c", probe], cwd=workspace, env={"PYTHONPATH": ""})
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    for key in ("file", "loop"):
        resolved = Path(payload[key]).resolve()
        if PACKAGE.resolve() in resolved.parents or resolved == PACKAGE:
            raise SystemExit(
                f"the imported package still comes from the source directory: {resolved}\n"
                "an editable install or a stray sys.path entry is masking the artifact"
            )
    if payload["version"] != _declared_version():
        raise SystemExit(f"wheel reports version {payload['version']}, expected {_declared_version()}")
    return payload["file"]


def _declared_version() -> str:
    for line in (PACKAGE / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version ="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("pyproject.toml has no version")


def run_suite_outside_the_tree(interpreter: Path, workspace: Path, test_wheelhouse: Path) -> str:
    """Copy the tests and examples out of the tree and run them there."""
    out = workspace / "outside"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PACKAGE / "tests", out / "tests", dirs_exist_ok=True)
    shutil.copytree(PACKAGE / "examples", out / "examples", dirs_exist_ok=True)
    # The tests' conftest inserts the *source* src directory; outside the tree
    # that directory does not exist, which is exactly what we want to prove.
    conftest = out / "tests" / "conftest.py"
    if conftest.is_file():
        conftest.unlink()

    # pytest is only needed to run the *copied* suite; if it cannot be installed
    # strictly offline the example run below still proves the installed artifact.
    run(
        [str(interpreter), "-m", "pip", "install", "--no-index", "--find-links", str(test_wheelhouse),
         "pytest", "pytest-asyncio"],
        cwd=out,
        check=False,
    )
    suite = run([str(interpreter), "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider"], cwd=out, check=False)
    summary = [line for line in suite.stdout.splitlines() if "passed" in line or "failed" in line]
    if suite.returncode != 0:
        failures = [line for line in suite.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
        raise SystemExit(
            "the copied suite failed against the installed artifact:\n"
            + "\n".join(failures[:20])
            + f"\n\nstdout:\n{suite.stdout[-4000:]}"
        )
    if suite.returncode == 5:
        raise SystemExit("no tests were collected outside the tree; the artifact shipped none?")

    failures: list[str] = []
    for example in sorted((out / "examples").glob("*.py")):
        completed = run([str(interpreter), str(example)], cwd=out, check=False)
        marker = "OK:" if completed.returncode == 0 else "FAILED:"
        log(f"  {marker} {example.name}: {completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else completed.stderr[-200:]}")
        if completed.returncode != 0:
            failures.append(example.name)
    if failures:
        raise SystemExit(f"examples failed outside the source tree: {failures}")
    return " | ".join(summary) if summary else "no pytest summary"


def sdist_manifest(path: Path) -> list[str]:
    with tarfile.open(path) as archive:
        return sorted(member.name.split("/", 1)[-1] for member in archive.getmembers() if member.isfile())


def wheel_manifest(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="forbid the network entirely; requires a prepared wheelhouse",
    )
    parser.add_argument(
        "--wheelhouse",
        default=None,
        help="directory holding the build dependencies (default: <workspace>/build-wheelhouse)",
    )
    args = parser.parse_args(argv)

    workspace = Path(tempfile.mkdtemp(prefix="yanxu-release-"))
    log(f"workspace: {workspace}")
    wheel_dir = workspace / "wheel"
    sdist_dir = workspace / "sdist"
    wheel_dir.mkdir()
    sdist_dir.mkdir()

    lookups = {"python": sys.version.split()[0], "package": str(PACKAGE)}
    lookups["version"] = _declared_version()
    wheelhouse = Path(args.wheelhouse) if args.wheelhouse else workspace / "build-wheelhouse"
    log(f"building {lookups['package']} {lookups['version']} with {lookups['python']}")
    log(f"build wheelhouse: {wheelhouse} ({', '.join(prepare_wheelhouse(wheelhouse, offline=args.offline))})")
    wheel, sdist = build(workspace, sdist_dir, wheel_dir, wheelhouse, offline=args.offline)
    log(f"wheel: {wheel.name} ({wheel.stat().st_size} bytes, sha256 {digest(wheel)})")
    log(f"sdist: {sdist.name} ({sdist.stat().st_size} bytes, sha256 {digest(sdist)})")

    for name, artifact in (("wheel", wheel), ("sdist", sdist)):
        venv_dir = workspace / f"venv-{name}"
        interpreter = make_venv(venv_dir)
        install(interpreter, artifact, wheelhouse)
        installed = assert_installed_outside_the_tree(interpreter, workspace)
        log(f"{name}: import comes from {installed}")
        summary = run_suite_outside_the_tree(interpreter, workspace / name, wheelhouse)
        log(f"{name}: {summary}")

    contents = wheel_manifest(wheel)
    names = {entry.rsplit("/", 1)[-1] for entry in contents}
    for required in ("METADATA", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        if required not in names:
            raise SystemExit(f"the wheel is missing {required}")
    if any(entry.startswith("tests/") for entry in contents):
        raise SystemExit("the wheel must not ship the test suite")
    for forbidden in ("SKILL.md.tmp", ".DS_Store"):
        if any(forbidden in entry for entry in contents):
            raise SystemExit(f"the wheel contains junk: {forbidden}")
    log(f"wheel contents: {len(contents)} files")

    sdist_files = sdist_manifest(sdist)
    for required in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        if required not in sdist_files:
            raise SystemExit(f"the sdist is missing {required}")
    log(f"sdist contents: {len(sdist_files)} files")

    if not args.keep:
        shutil.rmtree(workspace, ignore_errors=True)
    log("OK: built, installed outside the source tree, and ran the examples from the installed artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
