"""The build/CI scripts must be reproducible from this checkout and nowhere else.

`scripts/ci_check.py`, `scripts/verify_release.py` and
`scripts/export_public_snapshot.py` are the gates that decide whether the package
can be installed, so a wrong path in one of them is a wrong claim about the
release. Three properties are checked here, each one a real defect that was
observed in this repository:

* every path the gate derives is inside the checkout (the first version derived a
  "repository" from the *parent* of the package and cleaned up a directory that
  belonged to nobody);
* cleanup removes exactly the directory the run created, keeps it with
  `--keep-artifacts`, and never deletes a directory that already existed;
* the wheelhouse requirement list comes from `pyproject.toml`, and the command
  that prepares it survives a real shell (an unquoted `setuptools>=77.0.3` is a
  redirect, not a version constraint).

These read the repository's own scripts and packaging metadata, so they skip when
the tests have been copied out of the checkout (the release verifier does that).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from package_paths import PACKAGE_ROOT

PACKAGE = PACKAGE_ROOT.resolve()
SCRIPTS = PACKAGE / "scripts"
WORKFLOW = PACKAGE / ".github" / "workflows" / "harness.yml"
MANIFEST_NAME = "SNAPSHOT.json"

needs_checkout = pytest.mark.skipif(
    not all(
        (SCRIPTS / name).is_file()
        for name in ("ci_check.py", "verify_release.py", "export_public_snapshot.py")
    ),
    reason="构建脚本/打包清单不在这份检出里（安装产物中不存在）",
)

#: The dependencies a wheelhouse must satisfy, as normalized (PEP 503) names.
EXPECTED_REQUIREMENTS = {
    "setuptools",
    "wheel",
    "pytest",
    "pytest_asyncio",
    "build",
    "pyyaml",
    "pydantic",
}


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ci_check():
    return _load("yanxu_test_ci_check", SCRIPTS / "ci_check.py")


def verify_release():
    return _load("yanxu_test_verify_release", SCRIPTS / "verify_release.py")


def exporter():
    return _load("yanxu_test_exporter", SCRIPTS / "export_public_snapshot.py")


# --- paths and cleanup -------------------------------------------------------


@needs_checkout
def test_the_gates_derive_every_path_from_the_checkout() -> None:
    """No gate may point at a directory outside the package it is checking."""
    for module in (ci_check(), verify_release()):
        assert Path(module.PACKAGE).resolve() == PACKAGE
    # The scratch directory a run owns lives under the already-ignored build/.
    assert ci_check().DEFAULT_ARTIFACTS_DIR.resolve().is_relative_to(PACKAGE)


@needs_checkout
def test_cleanup_removes_only_the_directory_the_run_created(tmp_path: Path) -> None:
    gate = ci_check()

    # 1) a directory this run created is removed, and nothing next to it is.
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    artifacts = outside / "build" / "ci-check"
    created_dir, created_here = gate.prepare_artifacts(artifacts)
    assert created_here is True
    (created_dir / "release-1" / "wheel").mkdir(parents=True)
    (created_dir / "release-1" / "wheel" / "x.whl").write_text("x", encoding="utf-8")

    message = gate.cleanup_artifacts(created_dir, created_here=created_here, keep=False)
    assert "removed" in message
    assert not artifacts.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep me"

    # 2) a directory that already existed is never deleted.
    preexisting = outside / "keep-me"
    preexisting.mkdir()
    (preexisting / "data.txt").write_text("data", encoding="utf-8")
    directory, created = gate.prepare_artifacts(preexisting)
    assert created is False
    gate.cleanup_artifacts(directory, created_here=created, keep=False)
    assert (preexisting / "data.txt").read_text(encoding="utf-8") == "data"

    # 3) --keep-artifacts means the artifacts are actually kept.
    kept = outside / "kept"
    directory, created = gate.prepare_artifacts(kept)
    gate.cleanup_artifacts(directory, created_here=created, keep=True)
    assert kept.is_dir()


@needs_checkout
def test_offline_mode_names_the_dependencies_it_cannot_find(tmp_path: Path) -> None:
    """A missing dependency is a named failure, not a traceback inside a build."""
    verifier = verify_release()
    empty = tmp_path / "wheelhouse"
    empty.mkdir()

    assert verifier.missing_wheelhouse_requirements(empty) == verifier.declared_requirements()

    for name in sorted(EXPECTED_REQUIREMENTS):
        (empty / f"{name}-1.0-py3-none-any.whl").write_text("", encoding="utf-8")
    assert verifier.missing_wheelhouse_requirements(empty) == []


@needs_checkout
def test_the_wheelhouse_requirements_come_from_pyproject() -> None:
    verifier = verify_release()
    declared = {
        verifier._requirement_name(requirement) for requirement in verifier.declared_requirements()
    }
    assert declared == EXPECTED_REQUIREMENTS
    # The build backend's own floor is part of the list: offline mode resolves
    # everything through the wheelhouse, including the backend pip builds with.
    pyproject = (PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
    assert "setuptools>=77.0.3" in pyproject
    assert "setuptools>=77.0.3" in verifier.declared_requirements()


@needs_checkout
def test_a_relative_wheelhouse_is_not_resolved_inside_the_build_directory(tmp_path: Path) -> None:
    """`--wheelhouse build-wheelhouse` means "next to me", not "inside the temp build"."""
    verifier = verify_release()
    workspace = tmp_path / "workspace"
    resolved = verifier.resolve_wheelhouse("build-wheelhouse", workspace)

    assert resolved.is_absolute()
    assert resolved == (Path.cwd() / "build-wheelhouse").resolve()
    assert not resolved.is_relative_to(workspace.resolve())
    assert verifier.resolve_wheelhouse(None, workspace) == (workspace / "build-wheelhouse").resolve()


# --- what proves an installed artifact ---------------------------------------


@needs_checkout
def test_an_import_from_the_checkout_is_not_accepted_as_the_artifact(tmp_path: Path) -> None:
    """The install check must reject a source import *and* accept a build/ draft.

    The rule used to be "nothing under the package directory", which a gate run
    that keeps its artifacts in `build/` trips over even when the install is
    correct. It has to be specific about the source tree and about the venv.
    """
    verifier = verify_release()
    source_tree = PACKAGE / "src"
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"

    from_source = source_tree / "yanxu_harness" / "__init__.py"
    assert verifier.imported_file_problem(
        from_source, site_packages=site_packages, source_tree=source_tree
    )

    installed = site_packages / "yanxu_harness" / "__init__.py"
    assert verifier.imported_file_problem(
        installed, site_packages=site_packages, source_tree=source_tree
    ) is None

    # A correct install whose venv lives inside the repository (the gate keeps its
    # artifacts in `build/`) is still a correct install.
    under_the_repo = PACKAGE / "build" / "ci-check" / "release-1" / "venv" / "site-packages"
    assert verifier.imported_file_problem(
        under_the_repo / "yanxu_harness" / "__init__.py",
        site_packages=under_the_repo,
        source_tree=source_tree,
    ) is None

    # A `sys.path` entry that happens to expose the package from somewhere else is
    # not proof that the artifact was installed.
    elsewhere = tmp_path / "stray" / "yanxu_harness" / "__init__.py"
    assert verifier.imported_file_problem(
        elsewhere, site_packages=site_packages, source_tree=source_tree
    )


@needs_checkout
def test_a_distribution_may_not_ship_the_build_directory() -> None:
    verifier = verify_release()
    assert verifier.artifact_junk(
        [
            "yanxu_harness/__init__.py",
            "yanxu_harness.egg-info/SOURCES.txt",
            "build-wheelhouse/setuptools-84.0.0-py3-none-any.whl",
            "src/yanxu_harness/__pycache__/agent.pyc",
            ".pytest_cache/v/cache/nodeids",
        ]
    ) == [
        "yanxu_harness.egg-info/SOURCES.txt",
        "build-wheelhouse/setuptools-84.0.0-py3-none-any.whl",
        "src/yanxu_harness/__pycache__/agent.pyc",
        ".pytest_cache/v/cache/nodeids",
    ]
    assert verifier.artifact_junk(["yanxu_harness/__init__.py", "LICENSE"]) == []


@needs_checkout
def test_an_sdist_may_only_carry_its_own_manifest_under_egg_info() -> None:
    """`SOURCES.txt` is the sdist's own file list; the other egg-info files are not."""
    verifier = verify_release()
    manifest = "yanxu_harness-0.1.0a1/src/yanxu_harness.egg-info/SOURCES.txt"
    assert verifier.sdist_residue([manifest, "yanxu_harness-0.1.0a1/src/yanxu_harness/x.py"]) == []
    assert verifier.sdist_residue(
        [manifest, "yanxu_harness-0.1.0a1/src/yanxu_harness.egg-info/PKG-INFO"]
    ) == ["yanxu_harness-0.1.0a1/src/yanxu_harness.egg-info/PKG-INFO"]
    # MANIFEST.in is what keeps those pruned; without it the sdist ships them again.
    directives = [
        line.strip()
        for line in (PACKAGE / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert any(line.startswith("prune") and "egg-info" in line for line in directives), directives
    assert any(line.startswith("global-exclude") and ".py" in line for line in directives)


# --- dependency preparation --------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell quoting is what this checks")
@needs_checkout
def test_the_dependency_command_survives_a_real_shell(tmp_path: Path) -> None:
    """Every constraint must reach pip as one argument and leave no redirect file."""
    verifier = verify_release()
    command = verifier.wheelhouse_command_line("build-wheelhouse")

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    args_file = tmp_path / "argv.txt"
    stub = stub_dir / "python"
    stub.write_text(
        "#!/bin/sh\n" f'printf "%s\\n" "$@" > "{args_file}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    completed = subprocess.run(
        ["bash", "-c", command],
        cwd=str(run_dir),
        env={**os.environ, "PATH": os.pathsep.join([str(stub_dir), os.environ.get("PATH", "")])},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    argv = args_file.read_text(encoding="utf-8").splitlines()
    for requirement in verifier.declared_requirements():
        assert requirement in argv, f"{requirement} did not reach pip as one argument: {argv}"
    # An unquoted constraint would have made the shell create a file named "=77.0.3".
    assert sorted(path.name for path in run_dir.iterdir()) == []


@needs_checkout
def test_the_workflow_does_not_repeat_the_requirement_list() -> None:
    """The workflow prepares dependencies through the script, never through its own copy."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    # Comments may name the commands they explain; only executable lines count.
    commands = [line.split("#", 1)[0] for line in workflow.splitlines()]
    invocations = [line.strip() for line in commands if "pip download" in line]
    assert invocations == [], (
        "dependency preparation must go through `scripts/ci_check.py --prepare-wheelhouse`; "
        f"a second copy of the requirement list drifts from pyproject.toml: {invocations}"
    )
    assert "--prepare-wheelhouse" in workflow
    bare = [
        token
        for line in commands
        for token in re.findall(r"\S+", line)
        if ">=" in token and not token.startswith(("'", '"'))
    ]
    assert bare == [], f"these shell tokens would be read as a redirection: {bare}"


# --- packaging whitelist -----------------------------------------------------


@needs_checkout
def test_the_exporter_skips_build_residue() -> None:
    module = exporter()
    for junk in (
        "src/yanxu_harness.egg-info/SOURCES.txt",
        "src/yanxu_harness/__pycache__/agent.pyc",
        "docs/.DS_Store",
        ".pytest_cache/v/cache/nodeids",
    ):
        assert module._included(Path(junk)) is False, junk
    for shipped in (
        "docs/contract.md",
        "src/yanxu_harness/__init__.py",
        "scripts/ci_check.py",
        "examples/01_basic_dialogue.py",
    ):
        assert module._included(Path(shipped)) is True, shipped


@needs_checkout
def test_the_committed_manifest_matches_the_current_tree() -> None:
    """`SNAPSHOT.json` is regenerated, not hand-edited: a stale one is a failure.

    Refresh it with `python scripts/export_public_snapshot.py --refresh-manifest`.
    """
    module = exporter()
    path = PACKAGE / MANIFEST_NAME
    assert path.is_file(), f"{MANIFEST_NAME} is missing; refresh it from the exporter"
    manifest = json.loads(path.read_text(encoding="utf-8"))

    listed = {entry["path"]: entry for entry in manifest["files"]}
    expected = {item.relative_to(PACKAGE).as_posix() for item in module._iter_files()}
    assert set(listed) == expected, {
        "missing from the manifest": sorted(expected - set(listed)),
        "listed but not in the tree": sorted(set(listed) - expected),
    }

    stale = sorted(
        relative
        for relative, entry in listed.items()
        if hashlib.sha256((PACKAGE / relative).read_bytes()).hexdigest() != entry["sha256"]
    )
    assert stale == [], (
        "these files changed after the manifest was written; refresh it with "
        f"`python scripts/export_public_snapshot.py --refresh-manifest`: {stale}"
    )
    assert manifest["canonical_source"] == module.CANONICAL_SOURCE
    assert manifest["imported_from"] == module.IMPORTED_FROM
    assert not [name for name in listed if ".egg-info" in name]


# --- what the release report may claim ---------------------------------------


@needs_checkout
def test_the_verification_report_states_only_what_the_run_observed() -> None:
    """A generated report must not assert facts about machines it never saw.

    The first version of this report hardcoded "Python 3.10: not executed (no
    interpreter installed on the build machine)" — a claim about the environment,
    which was false on the machine that generated it. What a report may state is what
    the run itself observed: the interpreter, the platform, the commit, and the fact
    that other interpreters are outside its scope.
    """
    verifier = verify_release()
    lookups = {
        "python": "3.12.99",
        "package": "/tmp/example-package",
        "version": "9.9.9",
        "commit": "`" + "a" * 40 + "`",
        "tree": "clean at build time",
    }
    report = verifier._report(  # noqa: SLF001 - the report is the deliverable
        lookups,
        (),
        ["yanxu_harness/__init__.py"],
        ["pyproject.toml"],
        Path("SHA256SUMS"),
    )

    assert "3.12.99" in report, "the interpreter that actually ran is named"
    assert "a" * 40 in report, "the commit the artifacts were built from is recorded"
    for overreach in ("no interpreter installed", "not executed", "only 3.12"):
        assert overreach not in report, (
            f"the report must not claim {overreach!r}: it cannot know that"
        )
    # What it *may* say: other interpreters are outside this run's scope.
    assert "did **not** execute any other Python version" in report
    assert "docs/verification.md" in report, "the per-interpreter record is where to look"


@needs_checkout
def test_the_report_records_the_commit_and_tree_state_when_there_is_one() -> None:
    verifier = verify_release()
    commit, dirty = verifier._git_state(PACKAGE)  # noqa: SLF001
    assert commit is None or len(commit) == 40, commit
    assert isinstance(dirty, bool)
    if commit is not None:
        # In a checkout the report must carry a real revision; a dirty tree must be
        # visible rather than silently presented as reviewable.
        report = verifier._report(  # noqa: SLF001
            {
                "python": "3.12.99",
                "package": "/tmp/example-package",
                "version": "9.9.9",
                "commit": f"`{commit}`",
                "tree": "**dirty at build time**" if dirty else "clean at build time",
            },
            (),
            [],
            [],
            Path("SHA256SUMS"),
        )
        assert commit in report
        assert ("dirty at build time" in report) is dirty
