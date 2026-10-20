"""Explicit MCP contribution factory.

This is a thin, lazy join to the existing adapter. It does not implement a second
MCP client, a second ledger or a second lifecycle: opening calls the same
``McpClientManager``, ``discover_catalog`` and ``McpToolAdapter`` the direct API
uses, and the definitions it returns keep the adapter's frozen catalog guard.

Import rules: this module imports only standard-library and base harness types at
module scope. The MCP SDK (and ``anyio``/``jsonschema``) is imported inside
:meth:`McpExtensionFactory.open`, so a base install can import
``yanxu_harness.extensions`` without the extra and only an explicitly opened MCP
pack can fail with the documented install hint.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import HarnessError
from .errors import ExtensionRefused
from .types import ExtensionContribution, ExtensionContext

__all__ = ["McpExtensionFactory", "mcp_tool_factory"]


class _McpRefused(ExtensionRefused):
    """An MCP helper's own public-safe refusal (install hint, ownership, scope).

    The assembly wraps arbitrary factory exceptions so their messages cannot leak.
    This marker is only set by code in this module whose messages are constructed
    here from public facts, never from a host credential or factory repr.
    """

    public_safe = True


class McpExtensionFactory:
    """Owns (or explicitly borrows) one MCP manager for one deployment.

    ``manager=None`` creates and owns a manager. A borrowed manager must be
    declared with ``owned=False`` *and* must name the namespace it belongs to:
    the MCP connection key does not carry a namespace, so silently reusing a
    session across projects is exactly the leak this check prevents.
    """

    def __init__(
        self,
        *,
        config: Any,
        bindings: Mapping[str, Any],
        manager: Any = None,
        owned: bool | None = None,
        connection: Any = None,
        snapshot: Any = None,
        adapter: Any = None,
        deadline_seconds: Any = None,
        borrowed_namespace: str = "",
        skills: tuple[Any, ...] = (),
    ) -> None:
        if manager is None and owned is False and adapter is None:
            raise _McpRefused("a borrowed MCP manager must be supplied")
        if manager is not None and owned is None:
            raise _McpRefused(
                "an external MCP manager must declare ownership explicitly: pass owned=True "
                "to let the deployment close it, or owned=False to borrow it"
            )
        if manager is not None and owned is False and not borrowed_namespace:
            raise _McpRefused(
                "a borrowed MCP manager must name the namespace it belongs to; the MCP "
                "connection key has no namespace of its own"
            )
        if adapter is None and config is None:
            raise _McpRefused("an MCP factory needs a server configuration")
        if adapter is not None and not borrowed_namespace:
            raise _McpRefused(
                "a host-supplied MCP adapter must declare the namespace its borrowed "
                "resource belongs to; the adapter's connection key has no namespace"
            )
        self.config = config
        self.bindings = dict(bindings)
        self.manager = manager
        self.owned = manager is None if owned is None else bool(owned)
        self.connection = connection
        self.snapshot = snapshot
        self.adapter = adapter
        self.deadline_seconds = deadline_seconds
        self.borrowed_namespace = borrowed_namespace
        self.skills = tuple(skills)
        self._entered = False
        self._closed = False
        self._adapter: Any = adapter

    @classmethod
    def from_adapter(
        cls,
        adapter: Any,
        *,
        owned_manager: bool = False,
        manager: Any = None,
        borrowed_namespace: str = "",
    ) -> "McpExtensionFactory":
        """Use an already-built adapter (the host owns discovery and the manager)."""
        if manager is None:
            manager = getattr(adapter, "manager", None)
        return cls(
            config=None,
            bindings={},
            manager=manager,
            owned=owned_manager,
            adapter=adapter,
            borrowed_namespace=borrowed_namespace,
        )

    async def open(self, context: ExtensionContext) -> ExtensionContribution:
        if "mcp.tools.v1" not in context.capabilities:
            raise _McpRefused(
                f"{context.extension_id}: the binding did not grant mcp.tools.v1"
            )
        if self._closed:
            raise _McpRefused("this MCP factory was closed; create a new plan")
        self._validate_resource_binding(context)
        if self._adapter is not None:
            return ExtensionContribution(
                tools=tuple(self._adapter.definitions()), skills=self.skills
            )

        # Import only here: the base package must remain importable without the extra.
        from ..integrations.mcp import (
            McpClientManager,
            McpToolAdapter,
            discover_catalog,
        )
        from ..integrations.mcp.errors import McpExtraMissing
        from ..integrations.mcp.sdk import load_sdk

        try:
            if self.manager is None:
                # Load the SDK first: its error names the extra, whereas opening a
                # manager would fail on `anyio` with a bare ImportError.
                load_sdk()
                manager = McpClientManager()
                await manager.__aenter__()
                self.manager = manager
                self._entered = True
            else:
                manager = self.manager
            connection = self.connection or await manager.connect(self.config)
            snapshot = self.snapshot or await discover_catalog(
                manager,
                connection,
                config=self.config,
                bindings=self.bindings,
            )
            adapter = McpToolAdapter(
                manager=manager,
                connection=connection,
                config=self.config,
                snapshot=snapshot,
                deadline_seconds=self.deadline_seconds,
            )
            self.connection = connection
            self.snapshot = snapshot
            self._adapter = adapter
            return ExtensionContribution(
                tools=tuple(adapter.definitions()), skills=self.skills
            )
        except BaseException as exc:
            # A factory that acquired a manager and then failed must release it
            # before the assembly sees the exception.
            await self.close()
            if isinstance(exc, McpExtraMissing):
                # This message is generated by the SDK boundary and names the
                # install command; it is safe to surface verbatim. Arbitrary
                # factory errors are not copied into the public error.
                raise _McpRefused(str(exc)) from exc
            raise

    def _validate_resource_binding(self, context: ExtensionContext) -> None:
        """Prove that every MCP resource is bound to this deployment's scope.

        This runs before the adapter early return and before a new manager is
        entered, so no path can contribute definitions from a foreign connection.
        The namespace has no MCP-level key: an external manager/adapter/connection
        must therefore carry an explicit ``borrowed_namespace``. Authorization
        scope and config revision are compared on every available fact
        (configuration, frozen catalog and connection), not copied from the new
        deployment's binding.
        """
        adapter = self._adapter
        adapter_manager = getattr(adapter, "manager", None) if adapter is not None else None
        if self.manager is not None and adapter_manager is not None and adapter_manager is not self.manager:
            raise _McpRefused(
                f"{context.extension_id}: the declared MCP manager is not the adapter's "
                "actual manager"
            )
        manager = self.manager if self.manager is not None else adapter_manager
        config = getattr(adapter, "config", None) if adapter is not None else None
        if config is None:
            config = self.config
        snapshot = getattr(adapter, "snapshot", None) if adapter is not None else None
        if snapshot is None:
            snapshot = self.snapshot
        connection = getattr(adapter, "connection", None) if adapter is not None else None
        if connection is None:
            connection = self.connection

        external = (
            adapter is not None
            or manager is not None
            or snapshot is not None
            or connection is not None
        )
        if external:
            if not self.borrowed_namespace:
                raise _McpRefused(
                    f"{context.extension_id}: an external MCP resource must declare the "
                    "namespace it belongs to"
                )
            if self.borrowed_namespace != context.namespace:
                raise _McpRefused(
                    f"{context.extension_id}: the borrowed MCP resource belongs to "
                    f"namespace {self.borrowed_namespace!r}, not {context.namespace!r}"
                )

        sources = (
            (config, "configuration"),
            (snapshot, "catalog snapshot"),
            (connection, "connection"),
        )
        seen_scope = False
        seen_revision = False
        for source, label in sources:
            if source is None:
                continue
            scope = getattr(source, "authorization_scope", None)
            revision = getattr(source, "config_revision", None)
            if scope is not None:
                seen_scope = True
                if str(scope) != context.authorization_scope:
                    raise _McpRefused(
                        f"{context.extension_id}: the MCP {label} authorization scope "
                        "does not match the deployment binding"
                    )
            if revision is not None:
                seen_revision = True
                if str(revision) != context.config_revision:
                    raise _McpRefused(
                        f"{context.extension_id}: the MCP {label} configuration revision "
                        "does not match the deployment binding"
                    )
        if not (seen_scope and seen_revision):
            raise _McpRefused(
                f"{context.extension_id}: cannot verify the MCP resource's authorization "
                "scope and configuration revision"
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.owned or self.manager is None:
            return
        manager = self.manager
        self.manager = None
        try:
            await manager.__aexit__(None, None, None)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 - reported by the deployment close report
            if isinstance(exc, HarnessError):
                raise
            raise


def mcp_tool_factory(
    *,
    config: Any,
    bindings: Mapping[str, Any],
    manager: Any = None,
    owned: bool | None = None,
    connection: Any = None,
    snapshot: Any = None,
    deadline_seconds: Any = None,
    borrowed_namespace: str = "",
    skills: tuple[Any, ...] = (),
) -> McpExtensionFactory:
    """Build the explicit MCP factory used by an extension manifest/plan."""
    return McpExtensionFactory(
        config=config,
        bindings=bindings,
        manager=manager,
        owned=owned,
        connection=connection,
        snapshot=snapshot,
        deadline_seconds=deadline_seconds,
        borrowed_namespace=borrowed_namespace,
        skills=skills,
    )
