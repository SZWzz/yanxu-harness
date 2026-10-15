"""The dependency direction of this package, checked as code.

The harness is meant to be embeddable: its dependency closure is the standard
library plus whatever extra a host explicitly asked for. This test is the guard
that keeps it that way, and it mirrors the host-side boundary test so a shim that
re-exports harness code cannot smuggle the application layer back in.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys

import pytest
from pathlib import Path

from package_paths import PACKAGE_ROOT, is_checkout, source_root

SRC = source_root()
PACKAGE = SRC / "yanxu_harness"

#: Third-party or host modules that must never appear in the base import graph.
FORBIDDEN_TOP_LEVEL = {
    "backend",
    "storyide",
    "fastapi",
    "starlette",
    "uvicorn",
    "pydantic",
    "pydantic_ai",
    "httpx",
    "httpx2",
    "yaml",
    "aiofiles",
    "sqlalchemy",
    "pytest",
}

#: Modules allowed to import a forbidden name, and why. An immutable package name
#: is not "an application dependency"; every exemption here is a *lazy* import
#: inside a function, so the cold-process test below still proves the base import
#: does not need the extra.
ALLOWED_EXCEPTIONS: dict[str, set[str]] = {
    # The SKILL.md front matter parser. Optional on purpose: `import yaml` happens
    # inside a function and a missing extra raises a ConfigurationError naming the
    # extra instead of breaking `import yanxu_harness.skills`.
    "yanxu_harness.skills.local": {"yaml"},
}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(SRC).with_suffix("").parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append(node.module)
            elif node.level:
                # A relative import can only stay inside this package, but a
                # level that climbs past the package root would still be a bug.
                depth = node.level
                if path.parent == PACKAGE and depth > 1:
                    found.append(f"<escapes package: {node.module or ''}>")
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "attr", None) or getattr(
                getattr(node.func, "id", None), "__str__", lambda: ""
            )()
            if called in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append(first.value)
    return found


def _python_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_module_imports_the_application_layer() -> None:
    offenders: list[str] = []
    for path in _python_files():
        module = _module_name(path)
        allowed = ALLOWED_EXCEPTIONS.get(module, set())
        for imported in _imports(path):
            top = imported.split(".")[0]
            if top in FORBIDDEN_TOP_LEVEL and top not in allowed:
                offenders.append(f"{module}: imports {imported}")
    assert offenders == [], "the harness dependency direction was broken:\n" + "\n".join(offenders)


def test_every_exemption_is_a_lazy_import_not_a_module_level_one() -> None:
    """An exemption for a module-level import would defeat the cold-process check.

    `yaml` is allowed for the local skill provider only because the import sits
    inside a function; hoisting it to the top of the module would make the extra
    mandatory for every host.
    """
    for module, names in ALLOWED_EXCEPTIONS.items():
        path = (SRC / Path(*module.split("."))).with_suffix(".py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_level: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top_level.add(node.module.split(".")[0])
        assert not (names & top_level), (
            f"{module} imports {sorted(names & top_level)} at module level; "
            "an optional extra must be imported lazily"
        )
def test_the_fence_has_no_stale_exemptions() -> None:
    """An exemption for a module that no longer exists hides a later violation."""
    offenders = [module for module in ALLOWED_EXCEPTIONS if not (SRC / Path(*module.split("."))).with_suffix(".py").exists()]
    assert offenders == [], f"stale import-fence exemptions: {offenders}"


def test_every_submodule_imports_in_a_cold_process_without_the_application() -> None:
    """Import the whole package in a subprocess and report what it dragged in."""
    script = (
        "import json, pkgutil, sys, importlib;"
        "sys.path.insert(0, %r);"
        "import yanxu_harness;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(yanxu_harness.__path__, 'yanxu_harness.')];"
        "forbidden = %r;"
        "bad = sorted({name.split('.')[0] for name in sys.modules if name.split('.')[0] in forbidden});"
        "print(json.dumps(bad))"
    ) % (str(SRC), sorted(FORBIDDEN_TOP_LEVEL))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(SRC.parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"cold import failed: {result.stderr[-800:]}"
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_the_package_declares_no_runtime_dependency() -> None:
    """The base dependency list must stay empty; extras carry the optional bits."""
    pyproject = PACKAGE_ROOT / "pyproject.toml"
    if not is_checkout() or not pyproject.is_file():
        pytest.skip(
            "the dependency declaration lives in pyproject.toml, which an installed "
            "artifact does not ship; run this check from a checkout"
        )
    text = pyproject.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("dependencies = ["))
    block: list[str] = []
    if "]" not in lines[start][len("dependencies = [") :]:
        for line in lines[start + 1 :]:
            if line.startswith("]"):
                break
            block.append(line.strip())
    assert [entry for entry in block if entry] == [], (
        "the base install must not depend on anything outside the standard library"
    )
