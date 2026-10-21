"""Canonical, immutable data used by the extension identities.

Two rules shape this module:

* a frozen deployment must not change because the caller kept a reference to the
  dict it handed in, and
* two logically identical deployments must produce the same digest in two
  different processes.

The first is met by :class:`FrozenDict` and :func:`freeze_json`; the second by
:func:`extension_identity` and the versioned canonical mappings built here. No
identity contains a process id, an object address, a loop, a connection id, a
timestamp or a ``repr``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from ..versioning import fingerprint
from .errors import ManifestError, ExtensionRefused

__all__ = [
    "FrozenDict",
    "binding_identity",
    "definition_identity",
    "freeze_json",
    "manifest_identity",
    "skill_identity",
]


class FrozenDict(dict):
    """A JSON-serialisable mapping that refuses mutation after it was frozen.

    ``dict`` is kept as the base on purpose: the existing provider bridge and
    ``json`` encoder serialise a ``dict`` subclass without a custom hook, so a
    frozen schema can travel through the old paths unchanged. Every mutator is
    overridden, which is what ``MappingProxyType`` cannot offer (it is not
    JSON-serialisable and breaks the tool-declaration encoder).
    """

    def _frozen(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("this mapping is frozen; an assembled deployment cannot be mutated")

    __setitem__ = _frozen  # type: ignore[assignment]
    __delitem__ = _frozen  # type: ignore[assignment]
    clear = _frozen  # type: ignore[assignment]
    pop = _frozen  # type: ignore[assignment]
    popitem = _frozen  # type: ignore[assignment]
    setdefault = _frozen  # type: ignore[assignment]
    update = _frozen  # type: ignore[assignment]
    __ior__ = _frozen  # type: ignore[assignment]

    def copy(self) -> "FrozenDict":
        return self

    def __reduce__(self):  # pragma: no cover - only used by picklers, refused on purpose
        raise TypeError("a frozen deployment mapping is not picklable")


def freeze_json(value: Any, *, where: str = "value") -> Any:
    """Return a deeply immutable copy of a JSON value.

    Mappings become :class:`FrozenDict`, sequences become tuples (which ``json``
    still encodes as arrays), and scalars are returned unchanged after a finite
    number check. Anything that is not a JSON value is refused: a frozen
    identity that cannot be serialised is not an identity.
    """
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ManifestError(f"{where}: JSON object keys must be strings, got {type(key).__name__}")
            frozen[key] = freeze_json(item, where=f"{where}.{key}")
        return FrozenDict(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, where=f"{where}[{index}]") for index, item in enumerate(value))
    if isinstance(value, (str, bool)) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ManifestError(f"{where}: non-finite numbers are not allowed in a frozen deployment")
        return value
    raise ManifestError(f"{where}: {type(value).__name__} is not a JSON value")


def manifest_identity(manifest: Any) -> Mapping[str, Any]:
    """The versioned, public identity of one manifest (no display prose)."""
    return {
        "identity_version": 1,
        "manifest_schema": manifest.manifest_schema,
        "extension_api": manifest.extension_api,
        "id": manifest.id,
        "version": manifest.version,
        "requires_capabilities": sorted(manifest.requires_capabilities),
        "contributes": {
            "tools": list(manifest.contributes.tools),
            "skills": list(manifest.contributes.skills),
        },
    }


def binding_identity(binding: Any) -> Mapping[str, Any]:
    """The identity-bearing half of a binding.

    ``resource_refs`` are deliberately absent: they are opaque host references
    and their values must never enter a digest that is shown or persisted.
    """
    return {
        "identity_version": 1,
        "extension_id": binding.extension_id,
        "namespace": binding.namespace,
        "authorization_scope": binding.authorization_scope,
        "config_revision": binding.config_revision,
        "policy_revision": binding.policy_revision,
        "capabilities": sorted(binding.capabilities),
        "tools": list(binding.tools),
        "skills": list(binding.skills),
    }


def definition_identity(
    definition: Any,
    *,
    source: Mapping[str, Any] | None = None,
    schema: Mapping[str, Any] | None | object = ...,
) -> Mapping[str, Any]:
    """The execution-relevant identity of one tool definition.

    Handlers, validators and previews are callables, so their byte identity is
    not a verifiable release identity and is excluded. The definition version is
    included; the assembly combines it with the extension/binding digest before
    publishing, so a same-name/same-schema tool with a different extension
    version cannot reuse an old approval.
    """
    from ..tools.definition import Idempotency

    resolved_schema = definition.schema() if schema is ... else schema
    identity: dict[str, Any] = {
        "identity_version": 1,
        "name": definition.name,
        "description": definition.description,
        "version": definition.version,
        "effects": sorted(effect.value for effect in definition.effects),
        "requires": sorted(definition.requires),
        "execution_mode": definition.execution_mode.value,
        "idempotency": definition.idempotency.value,
        "result_char_limit": definition.result_char_limit,
        "capability": definition.capability,
        "args_schema": resolved_schema,
        "executable": definition.executable,
        "writes": definition.writes,
        "keyed": definition.idempotency is Idempotency.KEYED,
    }
    if source is not None:
        identity["source"] = dict(source)
    return identity


def skill_identity(
    metadata: Any,
    *,
    scope: str,
    source: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """The identity of one skill's frozen metadata (body is a fingerprint)."""
    identity = {
        "identity_version": 1,
        "skill_id": metadata.skill_id,
        "name": metadata.name,
        "description": metadata.description,
        "version": metadata.version,
        "when_to_use": metadata.when_to_use,
        "avoid_when": metadata.avoid_when,
        "tools": list(metadata.tools),
        "scope": scope,
        "fingerprint": metadata.fingerprint,
    }
    if source is not None:
        identity["source"] = dict(source)
    return identity


def digest(value: Any) -> str:
    """Short, stable digest used to build bounded effective versions."""
    return fingerprint(value).removeprefix("sha256:")[:12]
