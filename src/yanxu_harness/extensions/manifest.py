"""Strict parsing of the versioned extension manifest.

The parser is the first trust boundary. It accepts JSON text or an already
parsed mapping and then enforces, in order: byte size, duplicate keys, finite
numbers, depth/node bounds, the exact field set, field types and finally the
versioned identity rules. Nothing here imports a module named by the document,
starts a process, opens a socket or calls host code.

A display field that this build does not need is not silently ignored: only the
three whitelisted prose fields are accepted, and an unknown field is refused.
That is how ``authorization`` or ``python_path`` cannot arrive as decoration.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

from ..errors import ContractError
from .errors import ManifestError
from .types import (
    ContributionDeclarations,
    ExtensionLimits,
    ExtensionManifest,
    MANIFEST_SCHEMA_VERSIONS,
)

__all__ = ["MANIFEST_FIELDS", "parse_manifest"]

_REQUIRED_FIELDS = frozenset(
    {
        "manifest_schema",
        "extension_api",
        "id",
        "version",
        "requires_capabilities",
        "contributes",
    }
)
_DISPLAY_FIELDS = frozenset({"title", "description", "homepage"})
_CONTRIBUTION_FIELDS = frozenset({"tools", "skills"})

#: The exact top-level field set this build understands.
MANIFEST_FIELDS = frozenset(_REQUIRED_FIELDS | _DISPLAY_FIELDS)


def parse_manifest(
    payload: str | bytes | Mapping[str, Any],
    *,
    limits: ExtensionLimits | None = None,
) -> ExtensionManifest:
    """Parse one manifest or raise :class:`ManifestError`.

    ``Mapping`` input is copied; later mutation of the caller's dict (including
    nested lists) cannot change the returned manifest.
    """
    resolved_limits = limits or ExtensionLimits()
    if not isinstance(resolved_limits, ExtensionLimits):
        raise ContractError("limits must be an ExtensionLimits instance")

    if isinstance(payload, (str, bytes)):
        document = _parse_text(payload, resolved_limits)
    elif isinstance(payload, Mapping):
        document = _copy_mapping(payload)
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > resolved_limits.max_manifest_bytes:
            raise ManifestError(
                f"manifest is larger than {resolved_limits.max_manifest_bytes} bytes"
            )
    else:
        raise ManifestError(
            f"a manifest must be JSON text or a mapping, got {type(payload).__name__}"
        )

    _check_bounds(document, resolved_limits)
    return _build(document)


def _parse_text(payload: str | bytes, limits: ExtensionLimits) -> Mapping[str, Any]:
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError(f"manifest is not UTF-8: {exc}") from exc
        size = len(payload)
    else:
        text = payload
        size = len(payload.encode("utf-8"))
    if size > limits.max_manifest_bytes:
        raise ManifestError(f"manifest is larger than {limits.max_manifest_bytes} bytes")

    try:
        document = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ManifestError:
        raise
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ManifestError("a manifest must be a JSON object")
    return document


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key {key!r}; the parser will not guess which wins")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ManifestError(f"non-finite JSON number {value!r} is not allowed")


def _copy_mapping(value: Any, *, where: str = "manifest") -> Any:
    """A JSON-only deep copy that also rejects non-finite numbers and odd types."""
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ManifestError(f"{where}: JSON object keys must be strings")
            copied[key] = _copy_mapping(item, where=f"{where}.{key}")
        return copied
    if isinstance(value, (list, tuple)):
        return [_copy_mapping(item, where=f"{where}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (str, bool)) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ManifestError(f"{where}: non-finite numbers are not allowed")
        return value
    raise ManifestError(f"{where}: {type(value).__name__} is not a JSON value")


def _check_bounds(document: Any, limits: ExtensionLimits) -> None:
    max_depth = 0
    nodes = 0
    stack: list[tuple[Any, int]] = [(document, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_manifest_nodes:
            raise ManifestError(
                f"manifest has more than {limits.max_manifest_nodes} nodes"
            )
        if depth > limits.max_manifest_depth:
            raise ManifestError(
                f"manifest depth exceeds {limits.max_manifest_depth}"
            )
        max_depth = max(max_depth, depth)
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ManifestError("JSON object keys must be strings")
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ManifestError("non-finite numbers are not allowed")


def _build(document: Mapping[str, Any]) -> ExtensionManifest:
    unknown = sorted(set(document) - MANIFEST_FIELDS)
    if unknown:
        raise ManifestError(
            f"unknown manifest field(s): {', '.join(unknown)}; only versioned data is accepted"
        )
    missing = sorted(_REQUIRED_FIELDS - set(document))
    if missing:
        raise ManifestError(f"manifest is missing required field(s): {', '.join(missing)}")

    for name in ("manifest_schema", "extension_api"):
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManifestError(f"{name} must be an integer")

    schema = document["manifest_schema"]
    if schema not in MANIFEST_SCHEMA_VERSIONS:
        raise ManifestError(
            f"manifest_schema {schema!r} is not supported by this build "
            f"(supported: {MANIFEST_SCHEMA_VERSIONS})"
        )

    capabilities = document["requires_capabilities"]
    if not isinstance(capabilities, list):
        raise ManifestError("requires_capabilities must be an array of capability names")
    for capability in capabilities:
        if not isinstance(capability, str):
            raise ManifestError("requires_capabilities must contain only strings")
    if len(set(capabilities)) != len(capabilities):
        raise ManifestError("requires_capabilities contains a duplicate capability")

    contributes = document["contributes"]
    if not isinstance(contributes, Mapping):
        raise ManifestError("contributes must be an object")
    unknown_contributions = sorted(set(contributes) - _CONTRIBUTION_FIELDS)
    if unknown_contributions:
        raise ManifestError(
            f"unknown contribution field(s): {', '.join(unknown_contributions)}"
        )
    declared: dict[str, list[str]] = {}
    for name in ("tools", "skills"):
        raw = contributes.get(name, [])
        if not isinstance(raw, list):
            raise ManifestError(f"contributes.{name} must be an array of names")
        if any(not isinstance(item, str) for item in raw):
            raise ManifestError(f"contributes.{name} must contain only strings")
        declared[name] = list(raw)

    for name in ("title", "description", "homepage"):
        if name in document and not isinstance(document[name], str):
            raise ManifestError(f"{name} must be a string")

    try:
        return ExtensionManifest(
            id=document["id"],
            version=document["version"],
            manifest_schema=schema,
            extension_api=document["extension_api"],
            requires_capabilities=tuple(capabilities),
            contributes=ContributionDeclarations(
                tools=tuple(declared["tools"]),
                skills=tuple(declared["skills"]),
            ),
            title=document.get("title", ""),
            description=document.get("description", ""),
            homepage=document.get("homepage", ""),
        )
    except ContractError as exc:
        raise ManifestError(str(exc)) from exc
