"""Export a reviewable public snapshot of this package into an empty directory.

    python scripts/export_public_snapshot.py --out /tmp/yanxu-harness-snapshot

Everything is driven by an explicit **whitelist**. A file that is not named here
is not exported, so the failure mode is "missing from the snapshot" rather than
"a stray credential quietly published". The script then:

* refuses to write into a non-empty directory,
* refuses any source file that mentions a host-repository path, an author's
  absolute path, a credential-shaped literal or a private fixture directory,
* writes ``SNAPSHOT.json`` with a per-file sha256, the total size and one digest
  over the whole manifest, so a later reviewer can prove the build used this
  source and nothing else.

This produces a **candidate**. The canonical source is still
``packages/agent-harness`` in the host repository; two hand-edited copies would
drift, which is why the manifest exists.
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

#: Never exported, even if a directory above would otherwise include it.
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_PARTS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")

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
            if any(part in EXCLUDED_PARTS for part in path.parts):
                continue
            if path.suffix in EXCLUDED_SUFFIXES:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="destination directory (must be empty or absent)")
    parser.add_argument("--allow-warnings", action="store_true", help="report problems but still export")
    args = parser.parse_args(argv)

    destination = Path(args.out).expanduser().resolve()
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
        payload = target.read_bytes()
        entries.append(
            {
                "path": relative.as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    if problems and not args.allow_warnings:
        print("refusing to export: the snapshot looks like it contains host material", file=sys.stderr)
        for problem in problems[:40]:
            print(f"  {problem}", file=sys.stderr)
        shutil.rmtree(destination, ignore_errors=True)
        return 1
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)

    manifest = {
        "package": "yanxu-harness",
        "version": _version(),
        "status": "local candidate, unpublished",
        "canonical_source": "packages/agent-harness in the host repository",
        "files": entries,
        "total_bytes": sum(int(entry["bytes"]) for entry in entries),
    }
    blob = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    (destination / "SNAPSHOT.json").write_text(blob + "\n", encoding="utf-8")

    print(f"exported {len(entries)} files ({manifest['total_bytes']} bytes) to {destination}")
    print(f"snapshot digest: sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}")
    print("This is a candidate snapshot; the canonical source has not moved.")
    return 0


def _version() -> str:
    for line in (PACKAGE / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version ="):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
