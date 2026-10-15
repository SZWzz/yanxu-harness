"""Layer 3 of the tool model: declaration and discovery.

The model sees a *bounded* catalog: the tools a deployment has made visible,
each with a stable id, a compact description and its argument schema. When it
needs something that was not in the first batch, it asks for it by id, and the
request takes effect **in the next batch**. Nothing is granted by asking:

* asking is not approval — discovery never authorizes a write,
* a name the catalog does not contain is rejected with a reason, never
  invented,
* a request larger than the declared capacity is refused as a whole with an
  explicit ``over_capacity`` reason. Silently keeping the first N and reporting
  the rest as "not requested" would make an incomplete catalog look complete,
* a tool that was *not* declared in this batch stays discoverable later; the
  catalog is complete even when the declaration is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from ..errors import ContractError
from .registry import ToolCatalogEntry

__all__ = [
    "CapabilityCatalog",
    "CapabilityRequest",
    "DEFAULT_CAPACITY",
    "DiscoveryOutcome",
    "DiscoveryResult",
]

#: Local safety limit for one catalog/request. This is a *local* guard: it is not
#: a claim that any remote service accepts this many entries (plan section 3.2).
DEFAULT_CAPACITY = 64


class DiscoveryOutcome(str, Enum):
    """Why a capability request ended the way it did."""

    QUEUED = "queued"
    ACTIVE = "active"
    UNKNOWN_IDS = "unknown_ids"
    OVER_CAPACITY = "over_capacity"
    EMPTY = "empty"


@dataclass(frozen=True)
class CapabilityRequest:
    """One request for additional capabilities, made by one tool call."""

    call_id: str
    ids: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ContractError("a capability request must name the tool call that made it")
        object.__setattr__(self, "ids", tuple(dict.fromkeys(self.ids)))


@dataclass(frozen=True)
class DiscoveryResult:
    """What the catalog decided, in a shape a host can log verbatim."""

    outcome: DiscoveryOutcome
    #: Capabilities visible for the *next* batch.
    active: tuple[str, ...] = ()
    #: Requested capabilities that need the current batch to settle first.
    queued: tuple[str, ...] = ()
    #: Requested ids the catalog does not contain (or that the deployment hides).
    rejected: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in {DiscoveryOutcome.QUEUED, DiscoveryOutcome.ACTIVE, DiscoveryOutcome.EMPTY}


class CapabilityCatalog:
    """A bounded, id-stable view of the tools a deployment exposes."""

    def __init__(
        self,
        entries: Sequence[ToolCatalogEntry],
        *,
        capacity: int = DEFAULT_CAPACITY,
        declared: Sequence[str] | None = None,
    ) -> None:
        if capacity <= 0:
            raise ContractError("catalog capacity must be positive")
        by_id: dict[str, ToolCatalogEntry] = {}
        for entry in entries:
            if entry.name in by_id:
                raise ContractError(f"{entry.name}: duplicate capability id in catalog")
            by_id[entry.name] = entry
        self._entries = by_id
        self._capacity = capacity
        #: Deployment-visible ids. ``declared`` narrows it further (this batch).
        self._visible: set[str] = set(by_id)
        self._declared: set[str] = set(declared) if declared is not None else set(by_id)
        self._pending: set[str] = set()

    # -- reads ----------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def ids(self) -> tuple[str, ...]:
        """Every id this catalog can offer, in stable order."""
        return tuple(sorted(self._entries))

    def visible_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._visible))

    def declared_ids(self) -> tuple[str, ...]:
        """Ids declared to the model in the current batch."""
        return tuple(sorted(self._declared & self._visible))

    def entry(self, name: str) -> ToolCatalogEntry:
        try:
            return self._entries[name]
        except KeyError:
            raise ContractError(f"{name}: not a capability in this catalog") from None

    def entries(self, names: Sequence[str] | None = None) -> tuple[ToolCatalogEntry, ...]:
        chosen = self.ids() if names is None else tuple(sorted(set(names)))
        return tuple(self.entry(name) for name in chosen)

    # -- discovery ------------------------------------------------------

    def request(self, request: CapabilityRequest) -> DiscoveryResult:
        """Queue additional capabilities. Nothing becomes active in this batch."""
        if not request.ids:
            return DiscoveryResult(
                outcome=DiscoveryOutcome.EMPTY,
                active=self.declared_ids(),
                queued=tuple(sorted(self._pending)),
            )
        if len(request.ids) > self._capacity:
            return DiscoveryResult(
                outcome=DiscoveryOutcome.OVER_CAPACITY,
                active=self.declared_ids(),
                queued=tuple(sorted(self._pending)),
                rejected={
                    "capacity": (
                        f"requested {len(request.ids)} ids, capacity is {self._capacity}; "
                        "the request was refused as a whole, not truncated"
                    )
                },
            )
        unknown = [name for name in request.ids if name not in self._entries]
        if unknown:
            return DiscoveryResult(
                outcome=DiscoveryOutcome.UNKNOWN_IDS,
                active=self.declared_ids(),
                queued=tuple(sorted(self._pending)),
                rejected={name: "unknown capability id" for name in unknown},
            )
        hidden = [name for name in request.ids if name not in self._visible]
        if hidden:
            return DiscoveryResult(
                outcome=DiscoveryOutcome.UNKNOWN_IDS,
                active=self.declared_ids(),
                queued=tuple(sorted(self._pending)),
                rejected={name: "not exposed by this deployment" for name in hidden},
            )
        self._pending.update(request.ids)
        return DiscoveryResult(
            outcome=DiscoveryOutcome.QUEUED,
            active=self.declared_ids(),
            queued=tuple(sorted(self._pending)),
        )

    def promote(self) -> tuple[str, ...]:
        """Make queued capabilities active. Call this after the current batch settles."""
        self._declared |= self._pending
        self._pending.clear()
        return self.declared_ids()

    def hide(self, name: str) -> None:
        """Remove a capability from the deployment's visible set."""
        self._visible.discard(name)
        self._declared.discard(name)
        self._pending.discard(name)
