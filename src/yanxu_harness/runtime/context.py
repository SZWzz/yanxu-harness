"""Identity, authority and cancellation for one run.

The single rule this module exists to enforce: **server-side authority is never
derived from model output or client input.** Tool arguments, provider frames and
browser submissions may *repeat* an identity or a permission, but they can never
*establish* one. Concretely:

* :class:`Principal` is constructed by the host, and :meth:`Principal.from_untrusted`
  does not exist on purpose — there is no constructor that takes permissions from
  an untrusted mapping.
* :meth:`RunContext.for_tool_call` derives a child context that inherits the
  principal and the cancellation token unchanged; it cannot widen either.
* Every identifier a persisted operation needs (namespace, session, run,
  attempt, request, operation) lives here, so a durable backend does not have to
  invent its own names for the same things.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any, FrozenSet, Mapping

from ..errors import ContractError

__all__ = [
    "Cancellation",
    "Principal",
    "RunContext",
    "RunIdentity",
]


class Cancellation:
    """A cancellation token shared by one run and everything it started.

    The token is deliberately a plain object rather than an asyncio primitive so
    that a host may cancel from another thread (an HTTP request handler) and
    from another process boundary (a lease that expired).
    """

    __slots__ = ("_event", "_reason", "_lock")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str | None = None) -> None:
        """Cancel once. Later calls keep the first reason."""
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()

    def raise_if_cancelled(self) -> None:
        from ..tools.outcomes import ToolCancelled

        if self._event.is_set():
            raise ToolCancelled(self._reason or "run cancelled")


@dataclass(frozen=True)
class Principal:
    """Who the run acts as, and what the server allows it to do.

    ``permissions`` is a set of deployment-level capability names. It is part of
    the *server* authority context: a tool that reads it is reading policy, not
    model output.
    """

    subject: str
    permissions: FrozenSet[str] = frozenset()
    #: Which trusted component established this principal. Recorded so an audit
    #: can tell "the operator said so" from "the model said so".
    established_by: str = "server"

    def __post_init__(self) -> None:
        if not self.subject:
            raise ContractError("principal subject must be non-empty")
        if self.established_by != "server":
            raise ContractError(
                "a principal may only be established server-side; "
                f"got established_by={self.established_by!r}"
            )
        object.__setattr__(self, "permissions", frozenset(self.permissions))

    def allows(self, permission: str) -> bool:
        return permission in self.permissions


@dataclass(frozen=True)
class RunIdentity:
    """The identifiers one run must carry into storage and back."""

    namespace: str
    session_id: str
    run_id: str
    attempt_id: str
    request_id: str = ""
    operation_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "run_id", "attempt_id"):
            if not getattr(self, name):
                raise ContractError(f"run identity requires a non-empty {name}")

    def for_operation(self, operation_id: str) -> "RunIdentity":
        """Return the same identity stamped with one tool-operation id."""
        if not operation_id:
            raise ContractError("operation_id must be non-empty")
        return replace(self, operation_id=operation_id)

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "namespace": self.namespace,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class RunContext:
    """Everything a tool call or a provider request is allowed to see about the run."""

    identity: RunIdentity
    principal: Principal
    cancellation: Cancellation = field(default_factory=Cancellation)

    #: Host-owned services (story store, coordinator, clock, ...). Kept opaque:
    #: the harness passes it through so handlers can reach the host's own
    #: services without the harness importing them.
    services: Any = None

    #: Free-form host bookkeeping (model name, feature flags). Never authority.
    labels: Mapping[str, Any] = field(default_factory=dict)

    #: Tool-call identity, filled in by :meth:`for_tool_call`.
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise ContractError("RunContext.principal must be a Principal")
        if not isinstance(self.cancellation, Cancellation):
            raise ContractError("RunContext.cancellation must be a Cancellation")

    def for_tool_call(self, tool_call_id: str) -> "RunContext":
        """Derive the context of one tool call.

        The principal, the cancellation token and the services are inherited
        unchanged. Only the tool-call id differs, so a handler cannot obtain a
        wider authority by being called from a different place.
        """
        if not tool_call_id:
            raise ContractError("tool_call_id must be non-empty")
        return replace(self, tool_call_id=tool_call_id)

    def label(self, key: str, default: Any = None) -> Any:
        return self.labels.get(key, default)
