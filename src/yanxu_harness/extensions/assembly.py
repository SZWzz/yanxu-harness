"""Atomic assembly of a validated plan.

Opening is deliberately boring and ordered:

1. every factory is opened in extension-id order, and only a private list sees
   the result;
2. every contribution is checked against the manifest and the host binding;
3. only after all checks (names, dependencies, schemas, skill metadata, totals,
   report size) does one frozen registry/skill view become READY.

Any failure releases what was opened, including the factory that failed
mid-startup, and leaves the host's own registry untouched.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from ..errors import ContractError, HarnessError
from ..skills.provider import SkillLimits, SkillMetadata, normalize_scope
from ..tools.definition import ToolDefinition
from ..tools.registry import ToolRegistry
from ..versioning import fingerprint
from .canonical import (
    binding_identity,
    definition_identity,
    digest,
    freeze_json,
    manifest_identity,
    skill_identity,
)
from .errors import (
    ExtensionConflict,
    ExtensionError,
    ExtensionInvalidated,
    ExtensionLimitExceeded,
    ExtensionOpenFailed,
    ExtensionRefused,
    ExtensionStateError,
)
from .lifecycle import InvalidationToken, OwnerGuard, close_in_reverse
from .registry import FrozenToolRegistry
from .skills import CompositeSkillProvider, SkillEntry, metadata_size
from .types import (
    AssemblyPlan,
    CloseReport,
    ExtensionContext,
    ExtensionContribution,
    ExtensionState,
    SkillContribution,
    validate_contribution_type,
)

__all__ = ["AssemblySnapshot", "ExtensionDeployment", "open_extensions"]


@dataclass
class _Opened:
    entry: Any
    factory: Any
    contribution: ExtensionContribution


@dataclass
class _ExtensionRecord:
    extension_id: str
    version: str
    tools: list[tuple[str, str]]
    skills: list[str]


class ExtensionDeployment:
    """One deployment's resources, frozen snapshot and lifecycle.

    Use it as an async context manager::

        async with open_extensions(plan) as deployment:
            gateway = ToolGateway(registry=deployment.registry, ...)
            harness = deployment.bind_harness(
                stream_fn=stream_fn,
                gateway_bindings=GatewayBindings(gateway=gateway, context=context),
            )
    """

    def __init__(
        self,
        plan: AssemblyPlan,
        *,
        startup_timeout_seconds: float | None = None,
        close_timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(plan, AssemblyPlan):
            raise ContractError("open_extensions expects an AssemblyPlan")
        self._plan = plan
        self._startup_timeout = (
            plan.limits.startup_timeout_seconds
            if startup_timeout_seconds is None
            else float(startup_timeout_seconds)
        )
        self._close_timeout = (
            plan.limits.close_timeout_seconds
            if close_timeout_seconds is None
            else float(close_timeout_seconds)
        )
        if self._startup_timeout <= 0 or self._close_timeout <= 0:
            raise ContractError("startup and close timeouts must be positive")
        self._state = ExtensionState.NEW
        self._token = InvalidationToken()
        self._owner = OwnerGuard()
        self._opened: list[_Opened] = []
        self._registry: FrozenToolRegistry | None = None
        self._skills: CompositeSkillProvider | None = None
        self._deployment_fingerprint = ""
        self._report_base: Mapping[str, Any] | None = None
        self._close_report = CloseReport(state=ExtensionState.NEW)
        self._display_errors: list[str] = []

    # -- introspection --------------------------------------------------

    @property
    def plan(self) -> AssemblyPlan:
        return self._plan

    @property
    def state(self) -> ExtensionState:
        return self._state

    @property
    def fingerprint(self) -> str:
        return self._deployment_fingerprint

    @property
    def namespace(self) -> str:
        return self._plan.namespace

    @property
    def authorization_scope(self) -> str:
        return self._plan.authorization_scope

    @property
    def config_revision(self) -> str:
        return self._plan.config_revision

    @property
    def limits(self):
        return self._plan.limits

    @property
    def close_report(self) -> CloseReport:
        return self._close_report

    @property
    def registry(self) -> FrozenToolRegistry:
        if self._registry is None:
            raise ExtensionStateError(f"the deployment is {self._state.value}; no registry is published")
        return self._registry

    @property
    def skills(self) -> CompositeSkillProvider:
        if self._skills is None:
            raise ExtensionStateError(f"the deployment is {self._state.value}; no skills are published")
        return self._skills

    @property
    def invalidated_reason(self) -> str:
        return self._token.reason

    # -- open -----------------------------------------------------------

    async def __aenter__(self) -> "ExtensionDeployment":
        await self.open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        await self.aclose()
        return False

    async def open(self) -> "ExtensionDeployment":
        if self._state is ExtensionState.READY:
            return self
        if self._state is not ExtensionState.NEW:
            raise ExtensionStateError(
                f"cannot open a deployment in state {self._state.value}"
            )
        self._owner.bind()
        self._state = ExtensionState.OPENING
        opened: list[_Opened] = []
        try:
            for entry in self._plan.extensions:
                context = self._context_for(entry)
                factory = entry.factory
                try:
                    contribution = await asyncio.wait_for(
                        factory.open(context), timeout=self._startup_timeout
                    )
                    contribution = validate_contribution_type(contribution)
                except asyncio.CancelledError:
                    await self._close_failed_factory(entry, factory)
                    raise
                except Exception as exc:  # noqa: BLE001 - normalised into a public error
                    await self._close_failed_factory(entry, factory)
                    if isinstance(exc, HarnessError):
                        raise
                    raise ExtensionOpenFailed(
                        f"{entry.manifest.id}: factory failed during startup "
                        f"({type(exc).__name__}); the factory's own message is not copied "
                        "into the public error"
                    ) from exc
                opened.append(_Opened(entry, factory, contribution))
            await self._publish(opened)
        except BaseException as exc:  # noqa: BLE001 - every failure must clean up
            if opened:
                report = await self._close_opened(opened)
                self._close_report = report
            self._state = ExtensionState.FAILED
            if isinstance(exc, (HarnessError, asyncio.CancelledError)):
                raise
            raise ExtensionOpenFailed(
                f"the assembly failed after opening {len(opened)} factory(ies): "
                f"{type(exc).__name__}"
            ) from exc
        self._opened = opened
        self._state = ExtensionState.READY
        return self

    def _context_for(self, entry: Any) -> ExtensionContext:
        manifest = entry.manifest
        binding = entry.binding
        return ExtensionContext(
            extension_id=manifest.id,
            extension_version=manifest.version,
            namespace=binding.namespace,
            authorization_scope=binding.authorization_scope,
            config_revision=binding.config_revision,
            policy_revision=binding.policy_revision,
            capabilities=frozenset(binding.capabilities),
            tools=binding.tools,
            skills=binding.skills,
            resource_refs=binding.resource_refs,
            limits=self._plan.limits,
        )

    async def _close_failed_factory(self, entry: Any, factory: Any) -> None:
        close = getattr(factory, "close", None)
        if not callable(close):
            self._display_errors.append(
                f"{entry.manifest.id}: the failed factory has no close(); partial resources "
                "cannot be confirmed released"
            )
            return
        try:
            await asyncio.wait_for(close(), timeout=self._close_timeout)
        except Exception as exc:  # noqa: BLE001 - reported, not raised over the original failure
            self._display_errors.append(
                f"{entry.manifest.id}: close after startup failure failed "
                f"({type(exc).__name__})"
            )

    async def _close_opened(self, opened: Sequence[_Opened]) -> CloseReport:
        items = [
            (item.entry.manifest.id, getattr(item.factory, "close"))
            for item in opened
            if callable(getattr(item.factory, "close", None))
        ]
        report = await close_in_reverse(items, timeout_seconds=self._close_timeout)
        unconfirmed = list(report.unconfirmed)
        for item in opened:
            if not callable(getattr(item.factory, "close", None)):
                unconfirmed.append(item.entry.manifest.id)
        return CloseReport(
            released=report.released,
            failed=report.failed,
            unconfirmed=tuple(unconfirmed),
            state=report.state,
        )

    # -- validation and one-time publication ----------------------------

    async def _publish(self, opened: Sequence[_Opened]) -> None:
        tools, skill_entries, records, owner_of = await self._validate(opened)
        try:
            registry = FrozenToolRegistry(tools)
        except ContractError as exc:
            message = str(exc)
            if "already registered" in message:
                raise ExtensionConflict(message) from exc
            raise ExtensionRefused(f"the assembled tool set is invalid: {message}") from exc
        missing = registry.missing_dependencies()
        if missing:
            rendered = "; ".join(
                f"{name} requires {', '.join(dependencies)}"
                for name, dependencies in sorted(missing.items())
            )
            raise ExtensionRefused(f"the assembled tool set has unresolved dependencies: {rendered}")
        if len(tools) > self._plan.limits.max_tools:
            raise ExtensionLimitExceeded(
                f"{len(tools)} tools exceed max_tools={self._plan.limits.max_tools}"
            )
        if len(skill_entries) > self._plan.limits.max_skills:
            raise ExtensionLimitExceeded(
                f"{len(skill_entries)} skills exceed max_skills={self._plan.limits.max_skills}"
            )
        metadata_bytes = metadata_size(skill_entries)
        if metadata_bytes > self._plan.limits.max_skill_metadata_chars:
            raise ExtensionLimitExceeded(
                f"skill metadata is {metadata_bytes} chars, limit is "
                f"{self._plan.limits.max_skill_metadata_chars}"
            )
        skills = CompositeSkillProvider(
            scope=self._plan.namespace,
            entries=skill_entries,
            limits=self._plan.skill_limits,
            token=self._token,
            on_invalidate=self._invalidate_from_provider,
        )
        deployment_fingerprint = self._identity(tools, skill_entries, owner_of)
        report = self._build_report(records, tools, skill_entries, owner_of, deployment_fingerprint)
        self._check_report_size(report)

        self._registry = registry
        self._skills = skills
        self._deployment_fingerprint = deployment_fingerprint
        self._report_base = freeze_json(report, where="deployment report")

    async def _validate(
        self, opened: Sequence[_Opened]
    ) -> tuple[list[ToolDefinition], list[SkillEntry], list[_ExtensionRecord], dict[str, str]]:
        namespace = self._plan.namespace
        all_tools: list[ToolDefinition] = []
        owners: dict[str, str] = {}
        for definition in self._plan.local_tools:
            if definition.name in owners:
                raise ExtensionConflict(
                    f"tool name {definition.name!r} appears twice in the host tool set"
                )
            owners[definition.name] = "the host"
            all_tools.append(self._freeze_local(definition))

        skill_entries: list[SkillEntry] = []
        skill_keys: dict[tuple[str, str], str] = {}
        records: list[_ExtensionRecord] = []

        for opened_entry in opened:
            entry = opened_entry.entry
            contribution = opened_entry.contribution
            source = f"extension {entry.manifest.id}"
            source_digest = digest(
                {
                    "manifest": manifest_identity(entry.manifest),
                    "binding": binding_identity(entry.binding),
                }
            )
            record = _ExtensionRecord(
                extension_id=entry.manifest.id,
                version=entry.manifest.version,
                tools=[],
                skills=[],
            )

            supplied: dict[str, ToolDefinition] = {}
            for definition in contribution.tools:
                if definition.name in supplied:
                    raise ExtensionConflict(
                        f"{source} contributes tool {definition.name!r} more than once"
                    )
                supplied[definition.name] = definition
            bound_tools = set(entry.binding.tools)
            extra_tools = sorted(set(supplied) - bound_tools)
            if extra_tools:
                raise ExtensionRefused(
                    f"{source} contributed tool(s) the host did not bind: "
                    f"{', '.join(extra_tools)}"
                )
            missing_tools = sorted(bound_tools - set(supplied))
            if missing_tools:
                raise ExtensionRefused(
                    f"{source} did not contribute bound tool(s): {', '.join(missing_tools)}"
                )
            for name in entry.binding.tools:
                previous = owners.get(name)
                if previous is not None:
                    raise ExtensionConflict(
                        f"tool name {name!r} is claimed by {previous} and by {source}"
                    )
                definition = self._bind_definition(
                    supplied[name], entry=entry, source_digest=source_digest
                )
                owners[name] = entry.manifest.id
                all_tools.append(definition)
                record.tools.append((definition.name, definition.version))

            contributed_ids = [
                skill_id for item in contribution.skills for skill_id in item.skill_ids
            ]
            unbound_skills = sorted(set(contributed_ids) - set(entry.binding.skills))
            if unbound_skills:
                raise ExtensionRefused(
                    f"{source} contributed skill(s) the host did not bind: "
                    f"{', '.join(unbound_skills)}"
                )
            found_skills: set[str] = set()
            for item in contribution.skills:
                entries = await self._freeze_skill_contribution(
                    item,
                    source=source,
                    namespace=namespace,
                    allowed=entry.binding.skills,
                )
                for frozen in entries:
                    key = (namespace, frozen.skill_id)
                    previous = skill_keys.get(key)
                    if previous is not None:
                        raise ExtensionConflict(
                            f"skill {frozen.skill_id!r} in scope {namespace!r} is claimed by "
                            f"{previous} and by {source}"
                        )
                    skill_keys[key] = source
                    skill_entries.append(frozen)
                    found_skills.add(frozen.skill_id)
                    record.skills.append(frozen.skill_id)
            missing_skills = sorted(set(entry.binding.skills) - found_skills)
            if missing_skills:
                raise ExtensionRefused(
                    f"{source} did not contribute bound skill(s): {', '.join(missing_skills)}"
                )
            records.append(record)

        for index, item in enumerate(self._plan.host_skills):
            source = f"host skill source {index}"
            entries = await self._freeze_skill_contribution(
                item,
                source=source,
                namespace=namespace,
                allowed=None,
            )
            for frozen in entries:
                key = (namespace, frozen.skill_id)
                previous = skill_keys.get(key)
                if previous is not None:
                    raise ExtensionConflict(
                        f"skill {frozen.skill_id!r} in scope {namespace!r} is claimed by "
                        f"{previous} and by {source}"
                    )
                skill_keys[key] = source
                skill_entries.append(frozen)

        return all_tools, skill_entries, records, owners

    async def _freeze_skill_contribution(
        self,
        item: SkillContribution,
        *,
        source: str,
        namespace: str,
        allowed: Sequence[str] | None,
    ) -> list[SkillEntry]:
        source_scope = normalize_scope(item.scope or namespace)
        listed = await item.provider.list_metadata(scope=source_scope)
        by_id: dict[str, SkillMetadata] = {}
        for metadata in listed:
            if metadata.skill_id in by_id:
                raise ExtensionConflict(
                    f"{source} lists skill {metadata.skill_id!r} more than once in "
                    f"scope {source_scope!r}"
                )
            by_id[metadata.skill_id] = metadata
        if allowed is None:
            wanted = item.skill_ids or tuple(sorted(by_id))
        else:
            wanted = tuple(item.skill_ids) or tuple(allowed)
        missing = sorted(set(wanted) - set(by_id))
        if missing:
            raise ExtensionRefused(
                f"{source} declares skill(s) its provider did not list in scope "
                f"{source_scope!r}: {', '.join(missing)}"
            )
        entries: list[SkillEntry] = []
        for skill_id in wanted:
            metadata = by_id[skill_id]
            current = metadata.fingerprint or await item.provider.fingerprint(
                skill_id, scope=source_scope
            )
            if not current:
                raise ExtensionRefused(
                    f"{source}: skill {skill_id!r} has no content fingerprint, so a resume "
                    "could not prove it is reading the same method"
                )
            frozen = SkillMetadata(
                skill_id=metadata.skill_id,
                name=metadata.name,
                description=metadata.description,
                version=metadata.version,
                when_to_use=metadata.when_to_use,
                avoid_when=metadata.avoid_when,
                tools=tuple(metadata.tools),
                scope=namespace,
                fingerprint=current,
            )
            entries.append(
                SkillEntry(
                    skill_id=skill_id,
                    metadata=frozen,
                    fingerprint=current,
                    provider=item.provider,
                    source_scope=source_scope,
                    effective_scope=namespace,
                    source=source,
                )
            )
        return entries

    def _freeze_local(self, definition: ToolDefinition) -> ToolDefinition:
        schema = definition.schema() if callable(definition.args_schema) else definition.args_schema
        frozen_schema = (
            None
            if schema is None
            else freeze_json(schema, where=f"{definition.name} schema")
        )
        return replace(definition, args_schema=frozen_schema)

    def _bind_definition(
        self,
        definition: ToolDefinition,
        *,
        entry: Any,
        source_digest: str,
    ) -> ToolDefinition:
        schema = definition.schema() if callable(definition.args_schema) else definition.args_schema
        frozen_schema = (
            None
            if schema is None
            else freeze_json(schema, where=f"{definition.name} schema")
        )
        original_guard = definition.dispatch_guard
        skill_name = definition.name
        token = self._token

        def combined_guard(payload: Mapping[str, Any]) -> str | None:
            if not token.valid:
                return f"{skill_name}: deployment invalidated: {token.reason}"
            if original_guard is None:
                return None
            try:
                reason = original_guard(payload)
            except Exception as exc:  # noqa: BLE001 - a guard that cannot answer refuses
                return f"{skill_name}: the original dispatch guard raised {type(exc).__name__}"
            return str(reason) if reason else None

        effective_version = f"{definition.version}+ext.{source_digest}"
        return replace(
            definition,
            version=effective_version,
            args_schema=frozen_schema,
            dispatch_guard=combined_guard,
        )

    def _identity(
        self,
        tools: Sequence[ToolDefinition],
        skill_entries: Sequence[SkillEntry],
        owner_of: Mapping[str, str],
    ) -> str:
        per_extension: dict[str, dict[str, list[Any]]] = {
            entry.manifest.id: {"tools": [], "skills": []} for entry in self._plan.extensions
        }
        for definition in tools:
            source_id = owner_of.get(definition.name)
            if source_id in per_extension:
                per_extension[source_id]["tools"].append(
                    definition_identity(
                        definition,
                        source=per_extension_source(source_id, self._plan),
                    )
                )
        for entry in skill_entries:
            for planned in self._plan.extensions:
                if entry.source == f"extension {planned.manifest.id}":
                    per_extension[planned.manifest.id]["skills"].append(
                        skill_identity(
                            entry.metadata,
                            scope=entry.effective_scope,
                            source={
                                "kind": "extension",
                                "id": planned.manifest.id,
                                "version": planned.manifest.version,
                            },
                        )
                    )
                    break
        payload = {
            "deployment_version": 1,
            "plan_fingerprint": self._plan.fingerprint,
            "extensions": [
                {
                    "id": entry.manifest.id,
                    "version": entry.manifest.version,
                    "manifest": manifest_identity(entry.manifest),
                    "binding": binding_identity(entry.binding),
                    "tools": per_extension[entry.manifest.id]["tools"],
                    "skills": per_extension[entry.manifest.id]["skills"],
                }
                for entry in self._plan.extensions
            ],
            "host": {
                "tools": [
                    definition_identity(
                        definition,
                        source={"kind": "host"},
                    )
                    for definition in tools
                    if definition.name not in owner_of or owner_of[definition.name] == "the host"
                ],
                "skills": [
                    skill_identity(entry.metadata, scope=entry.effective_scope, source={"kind": "host"})
                    for entry in skill_entries
                    if entry.source.startswith("host skill source")
                ],
            },
            "limits": self._plan.limits.as_public(),
            "skill_limits": {
                "metadata_chars": self._plan.skill_limits.metadata_chars,
                "body_chars": self._plan.skill_limits.body_chars,
                "resource_bytes": self._plan.skill_limits.resource_bytes,
                "max_resources": self._plan.skill_limits.max_resources,
            },
        }
        return fingerprint(payload)

    def _build_report(
        self,
        records: Sequence[_ExtensionRecord],
        tools: Sequence[ToolDefinition],
        skill_entries: Sequence[SkillEntry],
        owner_of: Mapping[str, str],
        deployment_fingerprint: str,
    ) -> Mapping[str, Any]:
        host_tools = sorted(
            definition.name
            for definition in tools
            if owner_of.get(definition.name) == "the host"
        )
        return {
            "state": "ready",
            "fingerprint": deployment_fingerprint,
            "plan_fingerprint": self._plan.fingerprint,
            "scope": {
                "namespace": self._plan.namespace,
                "authorization_scope": self._plan.authorization_scope,
                "config_revision": self._plan.config_revision,
            },
            "limits": self._plan.limits.as_public(),
            "extensions": [
                {
                    "id": record.extension_id,
                    "version": record.version,
                    "tools": [{"name": name, "version": version} for name, version in record.tools],
                    "skills": sorted(record.skills),
                }
                for record in records
            ],
            "host": {
                "tools": host_tools,
                "skills": sorted(
                    entry.skill_id
                    for entry in skill_entries
                    if entry.source.startswith("host skill source")
                ),
            },
            "counts": {
                "extensions": len(records),
                "tools": len(tools),
                "skills": len(skill_entries),
            },
        }

    def _check_report_size(self, report: Mapping[str, Any]) -> None:
        raw = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > self._plan.limits.max_report_bytes:
            raise ExtensionLimitExceeded(
                f"the deployment report exceeds max_report_bytes="
                f"{self._plan.limits.max_report_bytes}; refusing to publish a truncated one"
            )

    def _invalidate_from_provider(self, reason: str) -> None:
        self._token.invalidate(reason)
        if self._state is ExtensionState.READY:
            self._state = ExtensionState.INVALIDATED

    # -- revocation and close ------------------------------------------

    def invalidate(self, reason: str = "") -> None:
        """Revoke use synchronously. Resources are released by :meth:`aclose`."""
        if self._state in (ExtensionState.CLOSED, ExtensionState.FAILED):
            return
        self._token.invalidate(reason or "the deployment was revoked by the host")
        self._state = ExtensionState.INVALIDATED

    async def aclose(self) -> CloseReport:
        if self._state is ExtensionState.CLOSED:
            return self._close_report
        if self._state is ExtensionState.NEW:
            self._state = ExtensionState.CLOSED
            self._token.invalidate("the deployment was closed before it was opened")
            self._close_report = CloseReport(state=ExtensionState.CLOSED)
            return self._close_report
        if self._state is ExtensionState.FAILED:
            self._state = ExtensionState.CLOSED
            return self._close_report
        self._owner.check("close")
        if self._state is not ExtensionState.CLOSING:
            self.invalidate("the deployment is closing")
            self._state = ExtensionState.CLOSING
        report = await self._close_opened(self._opened)
        self._close_report = report
        self._state = ExtensionState.CLOSED
        return report

    # -- published port -------------------------------------------------

    def report(self) -> Mapping[str, Any]:
        if self._report_base is None:
            raise ExtensionStateError(
                f"the deployment is {self._state.value}; no public report exists yet"
            )
        report = freeze_json(
            {**dict(self._report_base), "state": self._state.value},
            where="deployment report",
        )
        self._check_report_size(report)
        return report

    def binding(self) -> "_DeploymentBinding":
        if self._report_base is None:
            raise ExtensionStateError(
                f"the deployment is {self._state.value}; no deployment identity exists"
            )
        return _DeploymentBinding(self)

    def bind_harness(
        self,
        *,
        stream_fn: Any,
        gateway_bindings: Any = None,
        persistence: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Build a normal ``DefaultHarness`` bound to this frozen deployment.

        It does not add a second runner, gateway, approval store or ledger. The
        tools are the published registry's definitions adapted through the one
        gateway the host supplies; a gateway reading a different registry is
        refused instead of silently executing unfrozen definitions.
        """
        from ..runtime.default_harness import DefaultHarness
        from ..tools.core_bridge import core_tool

        if "tools" in kwargs or "deployment_binding" in kwargs:
            raise ContractError(
                "bind_harness owns tools and deployment_binding; pass the host's own "
                "tools to plan_extensions(local_tools=...) instead"
            )
        if gateway_bindings is None:
            raise ContractError(
                "bind_harness needs GatewayBindings for the deployment gateway"
            )
        gateway = getattr(gateway_bindings, "gateway", None)
        if gateway is None:
            raise ContractError("gateway_bindings must carry a gateway")
        if getattr(gateway, "registry", None) is not self.registry:
            raise ContractError(
                "the gateway reads a different registry than the frozen deployment; "
                "construct it with deployment.registry before binding the harness"
            )
        specs = tuple(
            core_tool(definition, gateway_bindings)
            for definition in self.registry.definitions()
        )
        return DefaultHarness(
            stream_fn=stream_fn,
            tools=specs,
            persistence=persistence,
            deployment_binding=self.binding(),
            **kwargs,
        )


AssemblySnapshot = ExtensionDeployment


def per_extension_source(extension_id: str, plan: AssemblyPlan) -> Mapping[str, Any]:
    for entry in plan.extensions:
        if entry.manifest.id == extension_id:
            return {
                "kind": "extension",
                "id": extension_id,
                "version": entry.manifest.version,
            }
    return {"kind": "host"}


class _DeploymentBinding:
    """The harness-facing port over one live deployment."""

    def __init__(self, deployment: ExtensionDeployment) -> None:
        self._deployment = deployment

    @property
    def fingerprint(self) -> str:
        return self._deployment.fingerprint

    @property
    def summary(self) -> Mapping[str, Any]:
        return freeze_json(
            {
                "deployment": self._deployment.fingerprint,
                "namespace": self._deployment.namespace,
                "authorization_scope": self._deployment.authorization_scope,
                "config_revision": self._deployment.config_revision,
                "extensions": [
                    {
                        "id": entry.manifest.id,
                        "version": entry.manifest.version,
                    }
                    for entry in self._deployment.plan.extensions
                ],
            },
            where="deployment binding summary",
        )

    def check(self, context: Any) -> str | None:
        """Return a refusal reason, or ``None`` when the context may use this deployment.

        No I/O: this is called before a model request or a tool dispatch, and it
        must stay cheap. The authorization scope is a host fact, so a context
        that does not declare it is refused rather than assumed to match.
        """
        if not self._deployment._token.valid:
            return self._deployment._token.reason
        if self._deployment.state is not ExtensionState.READY:
            return f"the deployment is {self._deployment.state.value}, not ready"
        identity = getattr(context, "identity", None)
        if identity is None:
            return "the run context carries no identity"
        if identity.namespace != self._deployment.namespace:
            return (
                f"run namespace {identity.namespace!r} does not match deployment namespace "
                f"{self._deployment.namespace!r}"
            )
        label = getattr(context, "label", None)
        scope = label("authorization_scope") if callable(label) else None
        if scope is None:
            return (
                "the run context does not declare an authorization_scope label; a deployment "
                "cannot assume it matches"
            )
        if str(scope) != self._deployment.authorization_scope:
            return (
                f"run authorization scope {scope!r} does not match deployment scope "
                f"{self._deployment.authorization_scope!r}"
            )
        return None


def open_extensions(
    plan: AssemblyPlan,
    *,
    startup_timeout_seconds: float | None = None,
    close_timeout_seconds: float | None = None,
) -> ExtensionDeployment:
    """Create the (not yet opened) deployment context manager for ``plan``."""
    return ExtensionDeployment(
        plan,
        startup_timeout_seconds=startup_timeout_seconds,
        close_timeout_seconds=close_timeout_seconds,
    )
