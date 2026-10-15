"""Export (or refresh) a reviewable public snapshot of this package.

    python scripts/export_public_snapshot.py --out /tmp/yanxu-harness-snapshot
    python scripts/export_public_snapshot.py --refresh-manifest

Everything is driven by an explicit **whitelist**. A file that is not named here
is not exported, so the failure mode is "missing from the snapshot" rather than
"a stray credential quietly published". The script then:

* refuses to write into a non-empty directory,
* refuses any source file that mentions a host-repository path, an author's
  absolute path, a credential-shaped literal or a private fixture directory,
* writes ``SNAPSHOT.json`` with a per-file sha256, the total size and one digest
  over the whole manifest, so a later reviewer can prove the build used this
  source and nothing else.

Two modes, one manifest format:

* ``--out DIR`` copies the whitelisted files and writes ``SNAPSHOT.json`` next to
  them. This is the publishable candidate.
* ``--refresh-manifest`` rewrites ``SNAPSHOT.json`` **in this repository** from the
  current tree. The committed manifest is therefore the repository's own record,
  regenerated instead of hand-edited, and a stale one is a failing test rather
  than a document that quietly lies about what the tree contains.

This package used to live at ``packages/agent-harness`` inside a host repository;
the export manifest keeps that origin as history, but the canonical source of the
files it lists is this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]

#: Exact files that ship. Directories are exported whole.
FILES = (
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
)
DIRECTORIES = (
    ".github",
    "LICENSES",
    "docs",
    "examples",
    "scripts",
    "src",
    "tests",
)

#: Never exported, even if a directory above would otherwise include it. The
#: egg-info and cache directories below are what an editable install and a test
#: run leave behind; a snapshot that shipped `src/*.egg-info` would publish one
#: machine's build metadata.
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_PARTS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".pi")
EXCLUDED_NAMES = (".DS_Store",)

#: Where the manifest's files came from, kept as provenance rather than deleted.
IMPORTED_FROM = "packages/agent-harness in the host repository (initial import commit 321ef4f)"
CANONICAL_SOURCE = "this repository (standalone yanxu-harness checkout)"

#: Paths that indicate the export accidentally contains host material.
FORBIDDEN_PATTERNS = (
    ("host repository", re.compile(r"/Volumes/etx/coding/StoryIDE|packages/agent-harness/src.*backend")),
    ("author home directory", re.compile(r"/Users/[A-Za-z0-9._-]+/")),
    ("private data directory", re.compile(r"data/\.storyide|fixtures/|_vault|manuscripts/")),
    ("credential-shaped literal", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b|\bBearer\s+[A-Za-z0-9._-]{20,}\b")),
)
#: Files whose *text* must satisfy the scan above (binaries are copied as bytes).
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt", ".in", ".cfg", ".sh"}

#: This file is excluded from its own scan: the pattern list below is the one
#: place that legitimately contains the strings it forbids. Excluding it by name
#: (rather than by "skip files that match") keeps a real violation anywhere else
#: a hard failure.
SELF = Path(__file__).resolve()

#: The manifest this script writes (and the one committed to the repository).
MANIFEST_NAME = "SNAPSHOT.json"


def _included(path: Path) -> bool:
    """Is this candidate part of the whitelist *and* free of build residue?"""
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if any(part.endswith(".egg-info") for part in path.parts):
        return False
    if path.suffix in EXCLUDED_SUFFIXES:
        return False
    if path.name in EXCLUDED_NAMES:
        return False
    return True


def _iter_files() -> list[Path]:
    found: list[Path] = []
    for name in FILES:
        candidate = PACKAGE / name
        if candidate.is_file():
            found.append(candidate)
    for name in DIRECTORIES:
        directory = PACKAGE / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if not _included(path):
                continue
            found.append(path)
    return sorted(set(found))


def _scan(path: Path) -> list[str]:
    if path.suffix not in TEXT_SUFFIXES or path.resolve() == SELF:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    problems: list[str] = []
    for label, pattern in FORBIDDEN_PATTERNS:
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            problems.append(f"{path.name}:{line}: looks like {label} ({match.group(0)[:60]!r})")
    return problems


def _entry(relative: str, payload: bytes) -> dict[str, object]:
    return {
        "path": relative,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _manifest(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "package": "yanxu-harness",
        "version": _version(),
        "status": "local candidate, unpublished",
        "canonical_source": CANONICAL_SOURCE,
        "imported_from": IMPORTED_FROM,
        "files": entries,
        "total_bytes": sum(int(entry["bytes"]) for entry in entries),
    }


def _serialize(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _report(problems: list[str], *, allow_warnings: bool) -> bool:
    """Print the scan result; return False when the export must be refused."""
    if not problems:
        return True
    if allow_warnings:
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        return True
    print("refusing to export: the snapshot looks like it contains host material", file=sys.stderr)
    for problem in problems[:40]:
        print(f"  {problem}", file=sys.stderr)
    return False


def _scan_tree() -> tuple[list[dict[str, object]], list[str]]:
    entries: list[dict[str, object]] = []
    problems: list[str] = []
    for source in _iter_files():
        problems.extend(_scan(source))
        entries.append(_entry(source.relative_to(PACKAGE).as_posix(), source.read_bytes()))
    return entries, problems


def refresh_manifest(*, allow_warnings: bool) -> int:
    """Rewrite the committed `SNAPSHOT.json` from the current tree."""
    entries, problems = _scan_tree()
    if not _report(problems, allow_warnings=allow_warnings):
        return 1
    manifest = _manifest(entries)
    blob = _serialize(manifest)
    (PACKAGE / MANIFEST_NAME).write_text(blob, encoding="utf-8")
    print(f"refreshed {MANIFEST_NAME}: {len(entries)} files ({manifest['total_bytes']} bytes)")
    print(f"snapshot digest: sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}")
    return 0


def export(destination: Path, *, allow_warnings: bool) -> int:
    if destination == PACKAGE or PACKAGE in destination.parents:
        raise SystemExit("refusing to export into the package itself")
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"{destination} is not empty; export into a fresh directory")
    destination.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    entries: list[dict[str, object]] = []
    for source in _iter_files():
        problems.extend(_scan(source))
        relative = source.relative_to(PACKAGE)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        # Digest the *copy*: this is what a reviewer would receive.
        entries.append(_entry(relative.as_posix(), target.read_bytes()))

    if not _report(problems, allow_warnings=allow_warnings):
        shutil.rmtree(destination, ignore_errors=True)
        return 1

    manifest = _manifest(entries)
    blob = _serialize(manifest)
    (destination / MANIFEST_NAME).write_text(blob, encoding="utf-8")
    print(f"exported {len(entries)} files ({manifest['total_bytes']} bytes) to {destination}")
    print(f"snapshot digest: sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}")
    print("This is a candidate snapshot; nothing has been published or tagged.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", default=None, help="destination directory (must be empty or absent)")
    mode.add_argument(
        "--refresh-manifest",
        action="store_true",
        help=f"rewrite {MANIFEST_NAME} in this repository from the current tree",
    )
    parser.add_argument("--allow-warnings", action="store_true", help="report problems but still export")
    args = parser.parse_args(argv)

    if args.refresh_manifest:
        return refresh_manifest(allow_warnings=args.allow_warnings)
    return export(Path(args.out).expanduser().resolve(), allow_warnings=args.allow_warnings)


def _version() -> str:
    for line in (PACKAGE / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version ="):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
