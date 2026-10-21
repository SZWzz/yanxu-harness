"""Errors raised by the extension protocol and assembly layer.

They all derive from :class:`~yanxu_harness.errors.HarnessError`, so a host that
already catches harness faults catches these without importing a second
taxonomy. The codes are stable enough to be persisted in a host audit record;
the messages name the extension and the refused operation, never a credential,
a factory ``repr`` or a traceback.
"""

from __future__ import annotations

from ..errors import HarnessError

__all__ = [
    "ExtensionConflict",
    "ExtensionError",
    "ExtensionInvalidated",
    "ExtensionLimitExceeded",
    "ExtensionOpenFailed",
    "ExtensionOwnerMismatch",
    "ExtensionRefused",
    "ExtensionStateError",
    "ManifestError",
]


class ExtensionError(HarnessError):
    """Base class for the extension layer."""

    code = "extension_error"


class ManifestError(ExtensionError):
    """A manifest is not the strict, bounded data this build accepts."""

    code = "extension_manifest_invalid"


class ExtensionRefused(ExtensionError):
    """The host's plan cannot be honoured (missing binding, unsupported request)."""

    code = "extension_refused"


class ExtensionConflict(ExtensionRefused):
    """Two sources claim the same stable name."""

    code = "extension_conflict"


class ExtensionLimitExceeded(ExtensionRefused):
    """A declared aggregate bound was exceeded; nothing was published."""

    code = "extension_limit_exceeded"


class ExtensionOpenFailed(ExtensionError):
    """Opening the plan failed; every resource the assembly knows about was released."""

    code = "extension_open_failed"


class ExtensionInvalidated(ExtensionError):
    """The deployment was revoked or closed; this reference may not be used."""

    code = "extension_invalidated"


class ExtensionOwnerMismatch(ExtensionError):
    """A lifecycle call came from a task/loop that does not own the resources."""

    code = "extension_owner_mismatch"


class ExtensionStateError(ExtensionError):
    """The requested lifecycle transition is not legal in the current state."""

    code = "extension_state_error"
