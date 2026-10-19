#!/usr/bin/env python3
"""MCP 安装门禁：基础安装、extra 环境、以及源码树外的 wheel/sdist 安装。

三条门分别回答三个问题：

* **base**：不带 SDK 的基础安装能不能导入全部子模块、能不能给出可照做的缺依赖错误、\n  基础示例还能不能跑；
* **extra**：装了锁定 extra 的环境里，MCP 全套（不含 skip）与独立探针、两个示例能否通过；
* **sdist**：同样的东西从 sdist 装出来还能不能通过。

用法（默认离线，使用已准备的 wheelhouse）：

    python scripts/mcp_gate.py --workdir build/mcp-gate --report build/mcp-gate/report.json

缺少 MCP wheelhouse 时加 ``--online`` 让 pip 自行解析（探针会如实记录走的是哪条路）。
Python 3.10 不在本机：那一条由 ``--python-floor`` 显式声明，默认记为阻塞而不是通过。
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

PACKAGE = Path(__file__).resolve().parents[1]
DEFAULT_WORKDIR = PACKAGE / "build" / "mcp-gate"
DEFAULT_WHEELHOUSE = Path("/tmp/mcp-wheelhouse-312")
BASE_WHEELHOUSE = PACKAGE / "build-wheelhouse"

_COLD_IMPORT = r"""
import importlib, json, pkgutil, sys
import yanxu_harness
for module in pkgutil.walk_packages(yanxu_harness.__path__, "yanxu_harness."):
    importlib.import_module(module.name)
banned = {"mcp", "mcp_types", "anyio", "httpx2", "jsonschema", "starlette", "uvicorn", "pydantic"}
leaked = sorted({name.split(".")[0] for name in sys.modules if name.split(".")[0] in banned})
print(json.dumps({"leaked": leaked, "modules": len([m for m in pkgutil.walk_packages(yanxu_harness.__path__, "yanxu_harness.")])}))
"""

_MISSING_EXTRA_SYNC = r"""
import json
from yanxu_harness.integrations.mcp import sdk_available
from yanxu_harness.integrations.mcp.errors import McpExtraMissing
from yanxu_harness.integrations.mcp.sdk import load_sdk

result = {"sdk_available": sdk_available(), "missing": False, "message": ""}
try:
    load_sdk()
except McpExtraMissing as exc:
    result["missing"] = True
    result["message"] = str(exc)
print(json.dumps(result))
"""


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 900.0):
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    child_env.pop("PYTHONPATH", None)
    if env:
        child_env.update(env)
    return subprocess.run(
        args, cwd=str(cwd), env=child_env, capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, check=False,
    )


def make_venv(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    venv.EnvBuilder(with_pip=True, symlinks=True).create(path)
    interpreter = path / ("Scripts" if os.name == "nt" else "bin") / "python"
    run([str(interpreter), "-m", "pip", "uninstall", "-y", "setuptools"], cwd=path)
    return interpreter


def build_artifacts(workdir: Path) -> tuple[Path, Path]:
    spec = importlib.util.spec_from_file_location("yanxu_verify_release", PACKAGE / "scripts" / "verify_release.py")
    assert spec and spec.loader
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    dist = workdir / "dist"
    wheel_dir, sdist_dir = dist / "wheel", dist / "sdist"
    for directory in (wheel_dir, sdist_dir):
        directory.mkdir(parents=True, exist_ok=True)
    wheel, sdist = verify.build(dist, sdist_dir, wheel_dir, BASE_WHEELHOUSE, offline=True)
    return wheel, sdist


def install(
    interpreter: Path,
    artifact: Path,
    *wheelhouses: Path,
    extra: str = "",
    online: bool = False,
    no_deps: bool = False,
    packages: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Install one artifact (optionally with extras) plus named extra packages.

    Every given wheelhouse is offered to pip; with at least one present the install is
    `--no-index`, so an offline gate cannot silently fall back to the network. `online`
    exists for a machine without a prepared MCP wheelhouse and is reported in the
    result rather than assumed.
    """
    available = [path for path in wheelhouses if path.is_dir()]
    offline = bool(available) and not online
    args = [str(interpreter), "-m", "pip", "install"]
    if offline:
        for path in available:
            args += ["--find-links", str(path)]
        args += ["--no-index"]
    if no_deps:
        args.append("--no-deps")
    target = f"{artifact}[{extra}]" if extra else str(artifact)
    args.append(target)
    args.extend(packages)
    return run(args, cwd=artifact.parent, timeout=1800.0)


def copy_suite(workdir: Path, name: str) -> Path:
    """Copy the suite (and the examples) next to a minimal pytest configuration.

    The checkout's pytest settings live in ``pyproject.toml``; a copied tree has no
    ``pyproject.toml``, and without ``asyncio_mode = auto`` every async test would
    fail for the wrong reason. The copy therefore gets the one setting it needs, and
    nothing else from the checkout.
    """
    target = workdir / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.copytree(PACKAGE / "tests", target / "tests")
    shutil.copytree(PACKAGE / "examples", target / "examples")
    (target / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\ntestpaths = tests\n", encoding="utf-8"
    )
    return target


def parse_pytest(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for line in output.splitlines()[::-1]:
        for key in counts:
            match = re.search(rf"(\d+) {key}", line)
            if match:
                counts[key] = int(match.group(1))
        if "passed" in line or "failed" in line or "error" in line:
            break
    return counts


def gate_base(workdir: Path, wheel: Path) -> dict:
    interpreter = make_venv(workdir / "venv-base")
    installed = install(interpreter, wheel, BASE_WHEELHOUSE, no_deps=True)
    detail: dict[str, object] = {"install_exit": installed.returncode}
    if installed.returncode != 0:
        detail["install_output"] = (installed.stdout + installed.stderr)[-800:]
        return {"ok": False, "detail": detail}

    cold = run([str(interpreter), "-c", _COLD_IMPORT], cwd=workdir)
    detail["cold_import"] = cold.stdout.strip().splitlines()[-1] if cold.returncode == 0 else cold.stderr[-500:]
    cold_ok = cold.returncode == 0 and '"leaked": []' in cold.stdout

    missing = run([str(interpreter), "-c", _MISSING_EXTRA_SYNC], cwd=workdir)
    missing_ok = missing.returncode == 0 and '"missing": true' in missing.stdout
    detail["missing_extra"] = missing.stdout.strip().splitlines()[-1] if missing.returncode == 0 else missing.stderr[-500:]

    example = run([str(interpreter), str(PACKAGE / "examples" / "01_basic_dialogue.py")], cwd=PACKAGE)
    detail["base_example_exit"] = example.returncode

    return {"ok": bool(cold_ok and missing_ok and example.returncode == 0), "detail": detail}


def gate_extra(workdir: Path, name: str, artifact: Path, wheelhouse: Path, *, online: bool) -> dict:
    interpreter = make_venv(workdir / f"venv-{name}")
    installed = install(
        interpreter,
        artifact,
        wheelhouse,
        BASE_WHEELHOUSE,
        extra="mcp",
        online=online,
        packages=("pytest", "pytest-asyncio"),
    )
    detail: dict[str, object] = {"artifact": artifact.name, "install_exit": installed.returncode}
    if installed.returncode != 0:
        detail["install_output"] = (installed.stdout + installed.stderr)[-800:]
        return {"ok": False, "detail": detail}

    suite_root = copy_suite(workdir, f"tree-{name}")
    suite = run(
        [str(interpreter), "-m", "pytest", "-q", "tests/mcp", "-p", "no:cacheprovider"],
        cwd=suite_root,
        timeout=1800.0,
    )
    counts = parse_pytest(suite.stdout)
    detail["mcp_suite"] = counts
    suite_ok = suite.returncode == 0 and counts["passed"] > 0 and counts["skipped"] == 0 and counts["failed"] == 0

    full = run(
        [str(interpreter), "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider"],
        cwd=suite_root,
        timeout=3600.0,
    )
    full_counts = parse_pytest(full.stdout)
    detail["full_suite"] = full_counts
    # The copied tree cannot run checkout-only tests; those are the skips we accept.
    full_ok = full.returncode == 0 and full_counts["failed"] == 0

    probe = run([str(interpreter), str(PACKAGE / "scripts" / "probe_mcp_acceptance.py")], cwd=PACKAGE, timeout=1200.0)
    probe_ok = probe.returncode == 0
    detail["probe_exit"] = probe.returncode
    detail["probe_tail"] = probe.stderr.strip().splitlines()[-1] if probe.stderr.strip() else ""

    examples = {}
    for example in ("11_mcp_stdio.py", "12_mcp_http_recovery.py"):
        completed = run([str(interpreter), str(PACKAGE / "examples" / example)], cwd=PACKAGE, timeout=600.0)
        examples[example] = completed.returncode
    detail["examples"] = examples
    examples_ok = all(code == 0 for code in examples.values())

    detail["installed_from"] = _installed_location(interpreter)
    return {"ok": bool(suite_ok and full_ok and probe_ok and examples_ok), "detail": detail}


def _installed_location(interpreter: Path) -> str:
    completed = run(
        [str(interpreter), "-c", "import yanxu_harness, pathlib; print(pathlib.Path(yanxu_harness.__file__).resolve())"],
        cwd=PACKAGE,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    parser.add_argument("--mcp-wheelhouse", default=str(DEFAULT_WHEELHOUSE))
    parser.add_argument("--online", action="store_true", help="let pip resolve the extra when the wheelhouse is absent")
    parser.add_argument("--report", default="")
    parser.add_argument("--skip-base", action="store_true")
    parser.add_argument("--skip-sdist", action="store_true")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    wheelhouse = Path(args.mcp_wheelhouse).expanduser()
    wheel, sdist = build_artifacts(workdir)

    report: dict[str, object] = {
        "artifacts": {"wheel": wheel.name, "sdist": sdist.name},
        "wheelhouse": str(wheelhouse),
        "python": sys.version.split()[0],
        "python_floor": {
            "ok": False,
            "detail": "no Python 3.10 interpreter on this machine; recorded as an environment blocker, "
            "not as a pass (see docs/mcp-client-delivery-report.zh-CN.md)",
        },
    }
    if not args.skip_base:
        report["base"] = gate_base(workdir, wheel)
    report["extra_wheel"] = gate_extra(workdir, "wheel", wheel, wheelhouse, online=args.online)
    if not args.skip_sdist:
        report["extra_sdist"] = gate_extra(workdir, "sdist", sdist, wheelhouse, online=args.online)

    gates = [
        value
        for key, value in report.items()
        if isinstance(value, dict) and "ok" in value and key != "python_floor"
    ]
    report["ok"] = all(gate["ok"] for gate in gates)

    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    print(
        f"BLOCKED python_floor {json.dumps(report['python_floor']['detail'])}",
        file=sys.stderr,
    )
    for key, value in report.items():
        if isinstance(value, dict) and "ok" in value and key != "python_floor":
            print(f"{'ok  ' if value['ok'] else 'FAIL'} {key} {json.dumps(value['detail'], sort_keys=True)}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
