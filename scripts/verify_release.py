"""Build this package and prove it works *outside its own source tree*.

    python scripts/verify_release.py [--keep] [--offline --wheelhouse DIR]
                               [--workspace DIR]

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
credential, Node.js or any other repository.

Dependency preparation derives the requirement list from ``pyproject.toml``
itself (build backend plus the extras this gate installs). A second hand-written
copy of that list in a script, a workflow or a document is how a build backend
floor and the thing that installs it drift apart, so there is exactly one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

#: The directory that is being released. Everything the verifier does is relative
#: to it, so the script works the same in a checkout and in an exported snapshot
#: (where there is no "parent repository" at all).
PACKAGE = Path(__file__).resolve().parents[1]

#: Extras the gate installs next to the base package. The release check claims to
#: exercise the adapters that need them, so they are part of the requirement set.
GATE_EXTRAS = ("dev", "yaml", "pydantic")

#: Path parts that must never appear inside a distribution. `build-wheelhouse` is
#: the directory a pipeline prepares next to the source tree; a wheel or sdist
#: that shipped it would publish someone's local dependency cache.
FORBIDDEN_ARTIFACT_PARTS = (
    "__pycache__",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pi",
    "build-wheelhouse",
    "build",
    "dist",
)


def artifact_junk(names: list[str]) -> list[str]:
    """Entries that prove a distribution was assembled from a working tree.

    A wheel/sdist is a release artifact: caches, egg-info metadata, bytecode and
    local dependency directories belong to the build machine, not to consumers.
    """
    junk: list[str] = []
    for name in names:
        parts = PurePosixPath(name).parts
        if any(part.endswith(".egg-info") for part in parts):
            junk.append(name)
            continue
        if any(part in FORBIDDEN_ARTIFACT_PARTS for part in parts):
            junk.append(name)
            continue
        if name.endswith((".pyc", ".pyo", ".DS_Store")) or name.endswith("/.DS_Store"):
            junk.append(name)
    return junk


def sdist_residue(names: list[str]) -> list[str]:
    """Build residue in an sdist, with one documented exception.

    setuptools always ships the sdist's own file list at
    ``<distribution>.egg-info/SOURCES.txt``: that file *is* the manifest, so it is
    not residue. Everything else under an egg-info directory is pruned by
    ``MANIFEST.in`` and must not come back.
    """
    return [name for name in artifact_junk(names) if not name.endswith(".egg-info/SOURCES.txt")]


def resolve_wheelhouse(value: str | None, workspace: Path) -> Path:
    """Resolve ``--wheelhouse`` *before* anything changes directory.

    A relative value such as the documented ``--wheelhouse build-wheelhouse`` is
    relative to the caller's working directory, not to the throwaway workspace
    the build runs in. Resolving it here is the difference between "the command
    in the README works" and "the build looks in a directory that cannot exist".
    """
    if value:
        return Path(value).expanduser().resolve()
    return (workspace / "build-wheelhouse").resolve()


def _toml_section(text: str, header: str) -> str:
    """The body of one TOML table, without a TOML parser (3.10 has no `tomllib`)."""
    start = text.index(header)
    rest = text[start + len(header) :]
    end = rest.find("\n[")
    return rest if end == -1 else rest[:end]


def _quoted_array(block: str, key: str) -> list[str]:
    match = re.search(rf'(?m)^{re.escape(key)}\s*=\s*\[(.*?)\]', block, re.S)
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def _requirement_name(requirement: str) -> str:
    name = re.split(r"[\s<>=!~;\[]", requirement.strip(), maxsplit=1)[0]
    return name.replace("-", "_").replace(".", "_").lower()


def _wheel_name(filename: str) -> str:
    return filename.split("-", 1)[0].replace("-", "_").replace(".", "_").lower()


def declared_requirements() -> list[str]:
    """Build backend + the gate's extras, read from ``pyproject.toml``.

    One source of truth on purpose: the workflow, the offline gate and the
    release verifier all ask this function what a wheelhouse must contain, so a
    raised floor in ``pyproject.toml`` cannot leave a stale copy behind.
    """
    text = (PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
    sources = [(_toml_section(text, "[build-system]"), "requires")]
    extras = _toml_section(text, "[project.optional-dependencies]")
    sources += [(extras, extra) for extra in GATE_EXTRAS]
    requirements: list[str] = []
    seen: set[str] = set()
    for source, key in sources:
        for requirement in _quoted_array(source, key):
            name = _requirement_name(requirement)
            if name in seen:
                continue
            seen.add(name)
            requirements.append(requirement)
    return requirements


def wheelhouse_command(directory: Path | str) -> list[str]:
    """The one command that prepares a wheelhouse, as an argument list."""
    return ["python", "-m", "pip", "download", "--only-binary=:all:", *declared_requirements(), "-d", str(directory)]


def wheelhouse_command_line(directory: Path | str) -> str:
    """The same command, quoted for a POSIX shell.

    An unquoted `setuptools>=77.0.3` is a redirect: the shell writes a file named
    `=77.0.3` and hands pip the unversioned `setuptools`. Quoting every constraint
    is what keeps dependency preparation from silently dropping its floors.
    """
    return shlex.join(wheelhouse_command(directory))


def wheel_distributions(wheelhouse: Path) -> set[str]:
    return {_wheel_name(path.name) for path in wheelhouse.glob("*.whl")}


def missing_wheelhouse_requirements(
    wheelhouse: Path, requirements: list[str] | None = None
) -> list[str]:
    """Declared requirements that no wheel in ``wheelhouse`` can satisfy.

    Offline mode resolves *everything* from here, so a partial wheelhouse must be
    a named failure ("missing: pytest, wheel") rather than a pip traceback deep
    inside an isolated build.
    """
    wanted = requirements if requirements is not None else declared_requirements()
    present = wheel_distributions(wheelhouse)
    return [requirement for requirement in wanted if _requirement_name(requirement) not in present]


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
    missing = missing_wheelhouse_requirements(wheelhouse)
    if missing and offline:
        raise SystemExit(
            f"--offline was requested but {wheelhouse} cannot satisfy: {', '.join(missing)}\n"
            f"prepare it first with: {wheelhouse_command_line(wheelhouse)}"
        )
    if missing:
        log(
            f"preparing the build wheelhouse in {wheelhouse} "
            "(this step is the only one that may use the network)"
        )
        # The dev extra runs the *copied* test suite in the throwaway venv, and the
        # optional extras let the release check exercise those adapters instead of
        # skipping them — all of it derived from pyproject.toml, not repeated here.
        run([sys.executable, *wheelhouse_command(wheelhouse)[1:]], cwd=wheelhouse)
        missing = missing_wheelhouse_requirements(wheelhouse)
        if missing:
            raise SystemExit(f"dependency preparation did not produce: {', '.join(missing)}")
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
        import setuptools  # noqa: F401 - imported to check it is importable

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


def imported_file_problem(imported: Path, *, site_packages: Path, source_tree: Path) -> str | None:
    """Why an imported file does not prove the artifact, or ``None`` if it does.

    Pure on purpose: the rule is the difference between "installed the wheel and
    proved it" and "the venv quietly imported the checkout", so it is tested
    directly instead of only through a multi-minute release run.
    """
    imported = imported.resolve()
    if not imported.is_relative_to(site_packages.resolve()):
        return (
            f"the imported package does not come from the installed artifact: {imported}\n"
            f"expected a file under {site_packages}"
        )
    if imported.is_relative_to(source_tree.resolve()):
        return (
            f"the imported package still comes from the source directory: {imported}\n"
            "an editable install or a stray sys.path entry is masking the artifact"
        )
    return None


def assert_installed_outside_the_tree(interpreter: Path, workspace: Path) -> str:
    """Prove the import came from the installed artifact, not from this checkout.

    Two things must hold, and the first version of this check only tested a
    weaker one of them:

    * the module comes from that interpreter's own site-packages — so the venv was
      not bypassed by a stray ``sys.path`` entry or a ``PYTHONPATH`` leak;
    * it does not come from this checkout's ``src/`` — which is what an editable
      install (or a run from the repository root) would otherwise hide.

    The check must not simply say "nothing under the package directory": the
    artifacts of a gate run live in ``build/`` on purpose, so a *correct* install
    would then be rejected for being inside the repository.
    """
    probe = (
        "import json, pathlib, sysconfig, yanxu_harness, yanxu_harness.core.agent_loop as loop;"
        "print(json.dumps({'file': yanxu_harness.__file__, 'version': yanxu_harness.__version__,"
        " 'loop': loop.__file__, 'site_packages': sysconfig.get_paths()['purelib']}))"
    )
    completed = run([str(interpreter), "-c", probe], cwd=workspace, env={"PYTHONPATH": ""})
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    site_packages = Path(payload["site_packages"])
    source_tree = PACKAGE / "src"
    for key in ("file", "loop"):
        problem = imported_file_problem(
            Path(payload[key]), site_packages=site_packages, source_tree=source_tree
        )
        if problem is not None:
            raise SystemExit(problem)
    if payload["version"] != _declared_version():
        raise SystemExit(f"wheel reports version {payload['version']}, expected {_declared_version()}")
    return payload["file"]


def _git_state(directory: Path) -> tuple[str | None, bool]:
    """The commit the artifacts were built from, and whether the tree was dirty.

    Read with a neutral git configuration so a host's `~/.gitconfig` cannot make the
    report fail or change its meaning. A directory that is not a git checkout returns
    ``(None, False)`` and the report says so, rather than inventing a revision.
    """
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(directory), capture_output=True, text=True, encoding="utf-8",
            env=env, check=False,
        )
        if revision.returncode != 0:
            return None, False
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(directory), capture_output=True, text=True, encoding="utf-8",
            env=env, check=False,
        )
        return revision.stdout.strip(), bool(status.stdout.strip())
    except OSError:
        return None, False


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
    parser.add_argument(
        "--workspace",
        default=None,
        help="place the build in this directory instead of a fresh temporary one",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="copy the artifacts here and write SHA256SUMS plus verification-report.md",
    )
    args = parser.parse_args(argv)

    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
        if workspace.exists() and any(workspace.iterdir()):
            raise SystemExit(f"--workspace {workspace} is not empty; use a fresh directory")
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = Path(tempfile.mkdtemp(prefix="yanxu-release-"))
    log(f"workspace: {workspace}")
    wheel_dir = workspace / "wheel"
    sdist_dir = workspace / "sdist"
    wheel_dir.mkdir()
    sdist_dir.mkdir()

    commit, dirty = _git_state(PACKAGE)
    lookups = {"python": sys.version.split()[0], "package": str(PACKAGE)}
    lookups["version"] = _declared_version()
    lookups["commit"] = f"`{commit}`" if commit else "unknown (not a git checkout)"
    lookups["tree"] = (
        "unknown"
        if commit is None
        else ("clean at build time" if not dirty else "**dirty at build time** (not a reviewable revision)")
    )
    wheelhouse = resolve_wheelhouse(args.wheelhouse, workspace)
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
    for forbidden_prefix in ("tests/", "examples/", "scripts/", "docs/"):
        if any(entry.startswith(forbidden_prefix) for entry in contents):
            raise SystemExit(
                f"the wheel must not ship {forbidden_prefix} (the sdist is the reviewable form)"
            )
    junk = artifact_junk(contents)
    if junk:
        raise SystemExit(f"the wheel contains build junk: {junk[:10]}")
    log(f"wheel contents: {len(contents)} files")

    sdist_files = sdist_manifest(sdist)
    for required in ("pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        if required not in sdist_files:
            raise SystemExit(f"the sdist is missing {required}")
    # The sdist is the reviewable form: it must carry the examples and the build
    # scripts (that is what MANIFEST.in promises), and the tests it runs.
    for required in ("examples/05_durable_approved_edit.py", "scripts/ci_check.py", "tests/conftest.py"):
        if required not in sdist_files:
            raise SystemExit(
                f"the sdist is missing {required}; MANIFEST.in and this check must agree "
                "on what a reviewer receives"
            )
    for required in ("README.md", "docs/contract.md", "docs/verification.md", "SNAPSHOT.json"):
        # SNAPSHOT.json is not in the sdist on purpose (it describes the repository);
        # everything else here is documentation a reader of the sdist can open.
        if required == "SNAPSHOT.json":
            continue
        if required not in sdist_files:
            raise SystemExit(f"the sdist is missing {required}")
    junk = sdist_residue(sdist_files)
    if junk:
        raise SystemExit(f"the sdist contains build junk: {junk[:10]}")
    log(f"sdist contents: {len(sdist_files)} files")

    if args.out:
        _publish(args.out, (("wheel", wheel), ("sdist", sdist)), lookups, contents, sdist_files)

    if not args.keep:
        shutil.rmtree(workspace, ignore_errors=True)
    log("OK: built, installed outside the source tree, and ran the examples from the installed artifacts.")
    return 0


def _publish(
    out: str,
    artifacts: Sequence[tuple[str, Path]],
    lookups: Mapping[str, Any],
    wheel_contents: Sequence[str],
    sdist_contents: Sequence[str],
) -> None:
    """Copy the artifacts and their evidence into a directory the user names."""
    destination = Path(out).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    copied: list[tuple[str, Path]] = []
    for label, path in artifacts:
        target = destination / path.name
        shutil.copy2(path, target)
        digest_value = digest(path)
        lines.append(f"{digest_value}  {target.name}")
        copied.append((label, target))
        log(f"{label}: copied to {target}")

    checksums = destination / "SHA256SUMS"
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = destination / "verification-report.md"
    report.write_text(
        _report(lookups, copied, wheel_contents, sdist_contents, checksums), encoding="utf-8"
    )
    log(f"wrote {checksums.name} and {report.name} in {destination}")


def _report(
    lookups: Mapping[str, Any],
    artifacts: Sequence[tuple[str, Path]],
    wheel_contents: Sequence[str],
    sdist_contents: Sequence[str],
    checksums: Path,
) -> str:
    import platform

    rows = "\n".join(
        f"| {label} | `{path.name}` | {path.stat().st_size} | `{digest(path)}` |"
        for label, path in artifacts
    )
    return f"""# Local verification report

Produced by `scripts/verify_release.py` for the `yanxu-harness` distribution,
version {lookups['version']}. This is a **local** verification: no artifact has been
uploaded anywhere.

## Environment and provenance

| Item | Value |
| --- | --- |
| Python | {lookups['python']} |
| implementation | {platform.python_implementation()} |
| platform | {platform.platform()} |
| built from | `{lookups['package']}` |
| version | `{lookups['version']}` |
| commit | {lookups['commit']} |
| working tree | {lookups['tree']} |

The commit is what `git rev-parse HEAD` reported in the directory the artifacts
were built from. If it is absent, this was not a git checkout and the artifacts
cannot be tied to a revision.

## Artifacts

| Kind | File | Bytes | sha256 |
| --- | --- | --- | --- |
{rows}

The digests are also in `{checksums.name}`.

## What was checked

* the wheel and the sdist were built with no index access (`--no-index` from the
  prepared wheelhouse);
* each was installed into a **throwaway virtual environment outside the source tree**,
  and the imported module was asserted to come from that environment's site-packages
  rather than from this checkout;
* the package's tests and the examples were copied out of the tree and run there
  against the installed artifact;
* the wheel contains no `tests/`, `examples/`, `scripts/` or `docs/` entries, no
  build residue and no junk files;
* the sdist contains its packaging metadata, documentation, examples, scripts and
  tests, and no build residue other than its own `SOURCES.txt` manifest.

## Contents

* wheel: {len(wheel_contents)} entries
* sdist: {len(sdist_contents)} entries

## Coverage of this run

This run says exactly what it observed and nothing about machines it did not see:

* it built and installed the artifacts with the interpreter named above, on the
  platform named above;
* it did **not** execute any other Python version or platform. Whether other
  versions work is recorded, per interpreter, in the package's
  `docs/verification.md` — not asserted here;
* it called no real model provider (the provider tests use a loopback server, and
  the optional real-provider script is outside the gate);
* it uploaded nothing: no index, no release page, no tag.

See `docs/verification.md` in the package for the failure-window matrix and the
per-interpreter record.
"""


if __name__ == "__main__":
    raise SystemExit(main())
