"""Versioning and fingerprint helpers.

Every persisted artifact this package touches carries a version, and every
value that an approval or a cache key depends on carries a fingerprint. Both
rules exist for the same reason: an unstamped artifact cannot be validated, and
an unvalidated artifact must not be silently upgraded.

Fingerprints are computed over *canonical JSON*: sorted keys, no insignificant
whitespace, ``ensure_ascii=False`` (so the digest does not depend on how a CJK
string was escaped), and a fixed algorithm prefix. Two values that differ in any
way that matters to a user produce different fingerprints, and the same value
produces the same fingerprint on any platform.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Collection, Mapping

from .errors import VersionRefused

__all__ = [
    "canonical_json",
    "fingerprint",
    "fingerprint_value",
    "require_version",
    "SUPPORTED_EVENT_VERSIONS",
    "SUPPORTED_CHECKPOINT_VERSIONS",
]

#: Event schema versions this build can read. Adding a version here is a
#: behavioural change and needs a test that reads real records of that version.
SUPPORTED_EVENT_VERSIONS: tuple[int, ...] = (1,)

#: Checkpoint/state schema versions this build can read.
SUPPORTED_CHECKPOINT_VERSIONS: tuple[int, ...] = (1,)


def canonical_json(value: Any) -> str:
    """Serialise ``value`` deterministically for hashing and for cache keys."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )


def _default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    raise TypeError(f"value of type {type(value).__name__} is not fingerprintable")


def fingerprint(value: Any) -> str:
    """Return ``sha256:<hex>`` over the canonical JSON of ``value``."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def fingerprint_value(value: Any) -> str:
    """Fingerprint a single value as if it were one argument."""
    if isinstance(value, Mapping):
        return fingerprint(dict(value))
    return fingerprint({"value": value})


def require_version(
    what: str,
    version: object,
    supported: Collection[int] = SUPPORTED_CHECKPOINT_VERSIONS,
) -> int:
    """Return ``version`` when this build can read it, else refuse.

    A *future* version and an *unknown* one are both refused. They are not the
    same situation — a future version means the writer is newer than the reader
    — but neither may be guessed at, and both must fail loudly instead of
    producing partially restored state.
    """
    if isinstance(version, bool) or not isinstance(version, int):
        raise VersionRefused(what, version, sorted(supported))
    if version not in supported:
        raise VersionRefused(what, version, sorted(supported))
    return version
