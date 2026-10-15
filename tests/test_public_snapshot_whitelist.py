"""The public-snapshot whitelist must carry everything the sdist promises publicly.

`MANIFEST.in` decides what a reviewer sees inside the *sdist*; the export
whitelist decides what a reviewer sees in the *public repository candidate*.
If the two drift, a file can be promised to sdist consumers and silently
missing from the snapshot an author would actually publish. This test makes
that drift a failure instead of a quiet omission, and it derives the expected
set from `MANIFEST.in` rather than from a second hard-coded list.
"""

from __future__ import annotations

import importlib.util

import pytest

from package_paths import PACKAGE_ROOT

PACKAGE = PACKAGE_ROOT
MANIFEST = PACKAGE / "MANIFEST.in"
EXPORTER = PACKAGE / "scripts" / "export_public_snapshot.py"

#: `MANIFEST.in` and `scripts/` live only in the canonical checkout: the release
#: verifier copies just `tests/` and `examples/` out of the tree, so from there
#: there is no packaging metadata to cross-check. Same convention as
#: `test_python_floor.py`'s workflow guard.
needs_checkout = pytest.mark.skipif(
    not (MANIFEST.is_file() and EXPORTER.is_file()),
    reason="独立包的打包元数据（MANIFEST.in/scripts）不在这份检出里",
)


def _load_exporter():
    spec = importlib.util.spec_from_file_location(
        "yanxu_export_public_snapshot",
        EXPORTER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest_directives() -> tuple[set[str], set[str]]:
    includes: set[str] = set()
    recursive: set[str] = set()
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "include" and len(parts) == 2:
            includes.add(parts[1])
        elif parts[0] == "recursive-include" and len(parts) >= 2:
            recursive.add(parts[1])
    return includes, recursive


@needs_checkout
def test_every_file_the_sdist_promises_is_also_in_the_public_snapshot() -> None:
    exporter = _load_exporter()
    includes, _ = _manifest_directives()

    for name in sorted(includes):
        # `include` entries are either a single shipped file (whitelisted in
        # FILES) or a whole exported directory.
        if name in exporter.FILES:
            continue
        if name in exporter.DIRECTORIES:
            continue
        raise AssertionError(
            f"MANIFEST.in ships {name!r} but the public snapshot whitelist "
            f"(FILES/DIRECTORIES) would omit it: FILES={exporter.FILES} "
            f"DIRECTORIES={exporter.DIRECTORIES}"
        )


@needs_checkout
def test_every_recursive_include_directory_is_exported() -> None:
    exporter = _load_exporter()
    _, recursive = _manifest_directives()

    missing = sorted(name for name in recursive if name not in exporter.DIRECTORIES)
    assert missing == [], (
        "MANIFEST.in recurses into directories the public snapshot whitelist "
        f"does not export: {missing}"
    )
