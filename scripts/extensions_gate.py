#!/usr/bin/env python3
"""Extension acceptance gate: base, MCP extra and out-of-tree wheel/sdist.

    python scripts/extensions_gate.py --workdir build/extensions-gate --report build/extensions-gate/report.json

Each environment installs the artifact built in this run, copies the suite and
examples out of the checkout, and runs from the copy. The import location is
recorded, so a gate cannot pass by importing the working tree.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parents[1]
DEFAULT_WORKDIR = PACKAGE / "build" / "extensions-gate"
WHEELHOUSE = PACKAGE / "build-wheelhouse"
MCP_ONLY_NODE = (
    "tests/extensions/test_e1_mcp.py::test_without_the_extra_the_failure_happens_only_when_the_pack_is_opened"
)


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 1800.0):
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    child_env.pop("PYTHONPATH", None)
    if env:
        child_env.update(env)
    return subprocess.run(
        args, cwd=str(cwd), env=child_env, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout, check=False,
    )


def make_venv(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    venv.EnvBuilder(with_pip=True, symlinks=True).create(path)
    interpreter = path / ("Scripts" if os.name == "nt" else "bin") / "python"
    run([str(interpreter), "-m", "pip", "uninstall", "-y", "setuptools"], cwd=path)
    return interpreter


def build_artifacts(workdir: Path) -> tuple[Path, Path]:
    spec = importlib.util.spec_from_file_location(
        "yanxu_verify_release", PACKAGE / "scripts" / "verify_release.py"
    )
    assert spec and spec.loader
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    dist = workdir / "dist"
    wheel_dir, sdist_dir = dist / "wheel", dist / "sdist"
    for directory in (wheel_dir, sdist_dir):
        directory.mkdir(parents=True, exist_ok=True)
    wheel, sdist = verify.build(dist, sdist_dir, wheel_dir, WHEELHOUSE, offline=True)
    return wheel, sdist


def install_pytest(interpreter: Path) -> subprocess.CompletedProcess:
    return run(
        [
            str(interpreter), "-m", "pip", "install", "--no-index",
            "--find-links", str(WHEELHOUSE), "pytest", "pytest-asyncio",
        ],
        cwd=WHEELHOUSE,
    )


def install_artifact(interpreter: Path, artifact: Path, *, extra: str = "", no_deps: bool = False):
    target = f"{artifact}[{extra}]" if extra else str(artifact)
    args = [
        str(interpreter), "-m", "pip", "install", "--no-index",
        "--find-links", str(WHEELHOUSE),
    ]
    if no_deps:
        args.append("--no-deps")
    args.append(target)
    return run(args, cwd=artifact.parent)


def copy_suite(workdir: Path, name: str) -> Path:
    target = workdir / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.copytree(PACKAGE / "tests", target / "tests")
    shutil.copytree(PACKAGE / "examples", target / "examples")
    shutil.copytree(PACKAGE / "scripts", target / "scripts")
    (target / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\ntestpaths = tests\n", encoding="utf-8"
    )
    return target


def parse_pytest(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "collected": 0}
    for line in output.splitlines()[::-1]:
        for key in counts:
            match = re.search(rf"(\d+) {key}", line)
            if match:
                counts[key] = int(match.group(1))
        if "passed" in line or "failed" in line or "error" in line:
            break
    return counts


def collected_nodes(interpreter: Path, root: Path, target: str) -> list[str]:
    completed = run(
        [str(interpreter), "-m", "pytest", "--collect-only", "-q", target, "-p", "no:cacheprovider"],
        cwd=root,
        timeout=600.0,
    )
    if completed.returncode not in (0, 5):
        return []
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    ]


def installed_location(interpreter: Path) -> str:
    completed = run(
        [
            str(interpreter), "-c",
            "import yanxu_harness, pathlib; print(pathlib.Path(yanxu_harness.__file__).resolve())",
        ],
        cwd=interpreter.parent,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def gate_base(workdir: Path, artifact: Path, *, name: str) -> dict[str, Any]:
    interpreter = make_venv(workdir / f"venv-{name}")
    installed = install_pytest(interpreter)
    installed_artifact = install_artifact(interpreter, artifact, no_deps=True)
    detail: dict[str, Any] = {
        "install_exit": installed.returncode,
        "artifact_install_exit": installed_artifact.returncode,
        "artifact": artifact.name,
    }
    if installed.returncode or installed_artifact.returncode:
        detail["install_output"] = (installed.stdout + installed.stderr)[-1200:]
        return {"ok": False, "detail": detail}

    root = copy_suite(workdir, f"tree-{name}")
    extension_nodes = collected_nodes(interpreter, root, "tests/extensions")
    mcp_nodes = collected_nodes(interpreter, root, "tests/extensions/test_e3_mcp_extension.py")
    detail["collected_extension_nodes"] = len(extension_nodes)
    detail["collected_mcp_nodes"] = len(mcp_nodes)
    collected_ok = len(extension_nodes) > len(mcp_nodes) > 0

    suite = run(
        [str(interpreter), "-m", "pytest", "-q", "tests/extensions", "-p", "no:cacheprovider"],
        cwd=root,
    )
    counts = parse_pytest(suite.stdout)
    detail["extension_suite"] = counts
    # In base the MCP capability nodes are the *only* allowed skips; the count
    # comes from collection, not from a hard-coded number.
    suite_ok = (
        suite.returncode == 0
        and counts["failed"] == 0
        and counts["errors"] == 0
        and counts["passed"] > 0
        and counts["skipped"] == len(mcp_nodes)
    )

    example = run(
        [str(interpreter), str(root / "examples" / "13_extension_tools_skills.py")],
        cwd=root,
        timeout=300.0,
    )
    detail["example_13_exit"] = example.returncode
    example_ok = example.returncode == 0 and '"status": "completed"' in example.stdout

    missing_extra = run(
        [str(interpreter), str(root / "examples" / "14_extension_mcp_recovery.py")],
        cwd=root,
        timeout=300.0,
    )
    detail["example_14_exit"] = missing_extra.returncode
    missing_ok = (
        missing_extra.returncode == 0 and '"status": "skipped"' in missing_extra.stdout
    )

    location = installed_location(interpreter)
    detail["installed_from"] = location
    detail["import_outside_tree"] = "site-packages" in location
    return {
        "ok": bool(collected_ok and suite_ok and example_ok and missing_ok),
        "detail": detail,
    }


def gate_extra(workdir: Path, artifact: Path) -> dict[str, Any]:
    interpreter = make_venv(workdir / "venv-extra")
    installed = install_pytest(interpreter)
    installed_artifact = install_artifact(interpreter, artifact, extra="mcp")
    detail: dict[str, Any] = {
        "install_exit": installed.returncode,
        "artifact_install_exit": installed_artifact.returncode,
        "artifact": artifact.name,
    }
    if installed.returncode or installed_artifact.returncode:
        detail["install_output"] = (installed.stdout + installed.stderr)[-1200:]
        return {"ok": False, "detail": detail}

    root = copy_suite(workdir, "tree-extra")
    suite = run(
        [
            str(interpreter), "-m", "pytest", "-q", "tests/extensions",
            "--deselect", MCP_ONLY_NODE, "-p", "no:cacheprovider",
        ],
        cwd=root,
    )
    counts = parse_pytest(suite.stdout)
    detail["extension_suite"] = counts
    extension_ok = (
        suite.returncode == 0
        and counts["failed"] == 0
        and counts["errors"] == 0
        and counts["passed"] > 0
        and counts["skipped"] == 0
    )

    mcp_suite = run(
        [str(interpreter), "-m", "pytest", "-q", "tests/mcp", "-p", "no:cacheprovider"],
        cwd=root,
        timeout=1800.0,
    )
    mcp_counts = parse_pytest(mcp_suite.stdout)
    detail["mcp_suite"] = mcp_counts
    mcp_ok = (
        mcp_suite.returncode == 0
        and mcp_counts["failed"] == 0
        and mcp_counts["errors"] == 0
        and mcp_counts["passed"] > 0
        and mcp_counts["skipped"] == 0
    )

    example = run(
        [str(interpreter), str(root / "examples" / "14_extension_mcp_recovery.py")],
        cwd=root,
        timeout=300.0,
    )
    detail["example_14_exit"] = example.returncode
    example_ok = (
        example.returncode == 0
        and '"status": "completed"' in example.stdout
        and '"bump_count": 1' in example.stdout
    )

    probe = run(
        [str(interpreter), str(root / "scripts" / "probe_extensions_acceptance.py")],
        cwd=root,
        timeout=900.0,
    )
    detail["probe_exit"] = probe.returncode
    probe_ok = probe.returncode == 0 and '"ok": true' in probe.stdout

    location = installed_location(interpreter)
    detail["installed_from"] = location
    detail["import_outside_tree"] = "site-packages" in location
    return {
        "ok": bool(extension_ok and mcp_ok and example_ok and probe_ok),
        "detail": detail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    parser.add_argument("--report", default="")
    parser.add_argument("--skip-sdist", action="store_true")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    wheel, sdist = build_artifacts(workdir)

    report: dict[str, Any] = {
        "artifacts": {"wheel": wheel.name, "sdist": sdist.name},
        "python": sys.version.split()[0],
        "wheelhouse": str(WHEELHOUSE),
        "python_310": "BLOCKED: no python3.10 interpreter on this machine"
        if shutil.which("python3.10") is None
        else shutil.which("python3.10"),
        "base": gate_base(workdir, wheel, name="base"),
        "extra": gate_extra(workdir, wheel),
    }
    if not args.skip_sdist:
        report["sdist"] = gate_base(workdir, sdist, name="sdist")
        report["sdist"]["detail"]["artifact"] = sdist.name
    else:
        report["sdist"] = {"ok": False, "detail": "skipped by --skip-sdist"}

    report["ok"] = bool(
        report["base"]["ok"] and report["extra"]["ok"] and report["sdist"]["ok"]
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
