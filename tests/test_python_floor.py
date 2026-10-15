"""声明的 Python 下界（3.10）要有牙：语法、标准库 API 与 CI 矩阵三面都要对得上。

`pyproject.toml` 写的是 `requires-python = ">=3.10"`，CI 矩阵也声明 3.10/3.11/3.12。
本机只有 3.12（只有它一个解释器；Docker 守护进程也未运行，见进度文档的环境阻塞记录），
所以「真的在 3.10 上跑过」这一格没法在本机产生。**没跑就不冒充跑过**——这组门做的是
本机能做到的最强静态验证：

* `ast.parse(feature_version=(3, 10))`：用 3.10 的语法规则编译每一个源文件。
  这是解释器自己的语法判定，不是我们自己列的清单，`except*`、`type X = ...`、
  未加 `from __future__` 的 3.12 泛型语法都会在这里现形。
* 3.11+ 独占 API/属性的静态扫描：`asyncio.TaskGroup`、`asyncio.timeout`、
  `typing.Self`、`datetime.UTC`、`tomllib`、`enum.StrEnum`、`itertools.batched`…
* 声明一致性：CI 矩阵必须包含 pyproject 的下界，且不许声明一个比下界更低的版本。

真正的多版本/跨平台实跑仍留在 CI 矩阵里，本文件不把「静态通过」写成「已在这些版本上验证」。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
SRC = PACKAGE / "src"
PYPROJECT = PACKAGE / "pyproject.toml"
WORKFLOW = PACKAGE / ".github" / "workflows" / "harness.yml"

FLOOR = (3, 10)

#: 3.11+ 才有的属性访问：(模块, 属性)
FORBIDDEN_ATTRIBUTES = {
    ("asyncio", "TaskGroup"),
    ("asyncio", "timeout"),
    ("asyncio", "timeout_at"),
    ("typing", "Self"),
    ("typing", "LiteralString"),
    ("typing", "assert_type"),
    ("typing", "Never"),
    ("typing", "TypeVarTuple"),
    ("dataclasses", "KW_ONLY"),
    ("datetime", "UTC"),
    ("enum", "StrEnum"),
    ("enum", "ReprEnum"),
    ("itertools", "batched"),
    ("contextlib", "aclosing"),
    ("pathlib", "Path"),  # 只有 `Path.walk` / `Path.glob` 的新关键字才是 3.11+，见下一个集合
}

#: 3.11+ 才有的独立名字/模块。
FORBIDDEN_NAMES = {"ExceptionGroup", "BaseExceptionGroup", "tomllib", "Self"}
FORBIDDEN_MODULES = {"tomllib", "asyncio.taskgroups"}

#: 3.11+ 才有的方法/属性（按属性名判，配合上面的模块集合使用）。
FORBIDDEN_METHODS = {
    ("Path", "walk"),  # 3.12
    ("Path", "copy_into"),  # 3.14，顺手挡住「从新文档抄来的 API」
}


def _python_files() -> list[Path]:
    roots = [SRC]
    for extra in ("tests", "examples", "scripts"):
        candidate = PACKAGE / extra
        if candidate.is_dir():
            roots.append(candidate)
    files: list[Path] = []
    for root in roots:
        files.extend(
            path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts
        )
    return files


def test_every_source_file_parses_under_the_declared_floor() -> None:
    """用 3.10 的语法规则编译每个文件 —— 语法下界由解释器自己判定。"""
    failures: list[str] = []
    files = _python_files()
    assert files, "没有找到任何源文件"
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            ast.parse(text, filename=str(path), feature_version=FLOOR)
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(PACKAGE)}:{exc.lineno}: {exc.msg}")
    assert failures == [], "这些文件在 3.10 上编译不过：\n" + "\n".join(failures)


def test_no_source_uses_a_311_only_attribute() -> None:
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if (node.value.id, node.attr) in FORBIDDEN_ATTRIBUTES and node.attr != "Path":
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {node.value.id}.{node.attr}")
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {node.id}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module, symbol in _imports_of(node):
                    if module in FORBIDDEN_MODULES or symbol in FORBIDDEN_NAMES:
                        offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} from {module} import {symbol}")
    assert offenders == [], "在 >=3.10 的声明下用了 3.11+ 独占 API：\n" + "\n".join(offenders)


def test_no_source_uses_a_newer_only_pathlib_method() -> None:
    """`Path.walk`（3.12）这类「从新文档抄来的方法」不会因为语法通过而暴露。"""
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                owner = node.value.id if isinstance(node.value, ast.Name) else None
                if owner in {"Path", "PurePath"} and (owner, node.attr) in FORBIDDEN_METHODS:
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {owner}.{node.attr}")
    assert offenders == [], "用了比 3.10 更新的 pathlib API：\n" + "\n".join(offenders)


def _imports_of(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    if isinstance(node, ast.Import):
        return [(alias.name, "") for alias in node.names]
    module = node.module or ""
    return [(module, alias.name) for alias in node.names]


def _require_pyproject() -> None:
    """这两组检查读的是**清单文件**：从安装产物里跑时它们不存在，于是明说跳过。

    `scripts/verify_release.py` 把 `tests/` 拷出源码树再跑，那里没有 `pyproject.toml`
    —— 缺清单不是缺陷，但「静态下界门」在那种场景下确实少跑几条，所以要写明。
    """
    if not PYPROJECT.is_file():
        pytest.skip(
            "pyproject.toml 不在安装产物里；下界的声明一致性只能在检出里核对"
        )


def _declared_floor() -> tuple[int, int]:
    _require_pyproject()
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', text)
    assert match, "pyproject.toml 没有可解析的 requires-python 下界"
    return int(match.group(1)), int(match.group(2))


def test_the_declared_floor_is_the_one_these_gates_use() -> None:
    assert _declared_floor() == FLOOR, (
        f"pyproject 声明的最低版本是 {_declared_floor()}，但本文件按 {FLOOR} 判定；"
        "两边必须一起改"
    )


def _extras_block() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    start = text.index("[project.optional-dependencies]")
    rest = text[start:]
    end = rest.find("\n[", 1)
    return rest if end == -1 else rest[:end]


def test_no_dependency_marker_silently_drops_the_declared_floor() -> None:
    """依赖不许用 `python_version` 标记把 3.10 排除掉。

    这条是按**环境标记**判的，不是按版本号数字判：`setuptools>=77.0`、
    `pydantic>=2.0`、`pyyaml>=6.0` 说的都是**包的版本**，和 Python 版本无关；
    把两者混在一起判是错的（第一版就这么写了，被 `setuptools>=77` 当场抓出来）。
    真正会「悄悄不支持 3.10」的写法只有一种：`; python_version >= "3.11"`。
    """
    _require_pyproject()
    block = _extras_block()
    offenders: list[str] = []
    for line in block.splitlines():
        marker = re.search(r';\s*python(?:_full)?_version\s*(>=|>|==)\s*"?(\d+)\.(\d+)', line)
        if not marker:
            continue
        wanted = (int(marker.group(2)), int(marker.group(3)))
        if wanted > FLOOR:
            offenders.append(line.strip())
    assert offenders == [], "这些依赖在 3.10 上会被标记排除，声明的下界就是假的：\n" + "\n".join(offenders)


def test_the_build_backend_requirement_is_declared() -> None:
    """构建后端要显式声明，否则 isolate 构建会去猜（并可能拿到一个不支持 3.10 的版本）。"""
    _require_pyproject()
    text = PYPROJECT.read_text(encoding="utf-8")
    block = text[text.index("[build-system]") :]
    assert "requires" in block and "setuptools" in block and "wheel" in block


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="独立 CI 工作流不在这份检出里")
def test_the_ci_matrix_covers_the_declared_floor() -> None:
    """CI 不许悄悄停止测下界 —— 声明支持 3.10 就必须有一个 job 真跑 3.10。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    floor_text = f'"{FLOOR[0]}.{FLOOR[1]}"'
    assert floor_text in text, f"CI 矩阵里没有 {floor_text}，声明的下界没人测"
    versions = set(re.findall(r'"(3\.\d+)"', text))
    assert versions, "CI 矩阵里没有版本字符串"
    lowest = min(tuple(int(part) for part in version.split(".")) for version in versions)
    assert lowest == FLOOR, f"CI 最低测到 {lowest}，比声明的下界 {FLOOR} 还低（那是在测不支持的版本）"


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="独立 CI 工作流不在这份检出里")
def test_the_ci_matrix_runs_on_more_than_one_platform() -> None:
    """AC43 要的不只是版本矩阵，还要一条跨平台切片。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    for needed in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert needed in text, f"CI 矩阵缺 {needed}"


# --- 仪器自证：这组门本身有没有牙 -----------------------------------------


def test_the_syntax_gate_rejects_newer_syntax() -> None:
    """证明 `feature_version=(3, 10)` 真的在判语法，而不是一个恒过的装饰。

    没有这条，「每个文件都能在 3.10 编译」可能只是因为它其实按当前解释器在判 ——
    那样一来 3.11/3.12 的语法会一路绿灯，声明的下界就成了空话。
    """
    for label, source in (
        ("except* (3.11)", "try:\n    pass\nexcept* ValueError:\n    pass\n"),
        ("type statement (3.12)", "type Alias = int\n"),
    ):
        with pytest.raises(SyntaxError):
            ast.parse(source, feature_version=FLOOR)
    # 反过来：下界自己的语法必须被接受，否则这条门会把支持的写法也拦掉。
    for label, source in (
        ("match (3.10)", "match 1:\n    case 1:\n        pass\n"),
        ("parenthesised context managers (3.10)", "with (open('a') as f, open('b') as g):\n    pass\n"),
        ("PEP 604 unions (3.10)", "def f(x: int | None) -> str | None:\n    return None\n"),
    ):
        ast.parse(source, feature_version=FLOOR)


def test_the_api_scan_rejects_a_known_newer_api() -> None:
    """API 扫描也要自证：喂它一段 3.11+ 的代码，必须报出来。"""
    sample = "import asyncio\nasync def f():\n    async with asyncio.TaskGroup() as group:\n        pass\n"
    tree = ast.parse(sample)
    found = [
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id, node.attr) in FORBIDDEN_ATTRIBUTES
    ]
    assert "asyncio.TaskGroup" in found
