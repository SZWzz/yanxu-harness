"""One extension deployment a parent test and a child process can both build.

The identity inputs are fixed here so a real cross-process test can prove that the
*same* logical deployment (same manifest, binding, tool contract and limits)
produces the same digest in two interpreters. Only the handler object and the
stream differ; neither participates in the deployment identity.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall
from yanxu_harness.extensions import (
    ContributionDeclarations,
    ExtensionBinding,
    ExtensionContribution,
    ExtensionLimits,
    ExtensionManifest,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
)
from yanxu_harness.skills.provider import SkillLimits
from yanxu_harness.storage import (
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.tools import (
    ConservativePolicy,
    ExecutionMode,
    GatewayBindings,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    core_tool,
)
from yanxu_harness.tools.outcomes import ToolArgsInvalid

NAMESPACE = "project-a"
AUTHORIZATION_SCOPE = "authorized"
CONFIG_REVISION = "cfg-1"
POLICY_REVISION = "pol-1"
EXTENSION_ID = "reference.pack"
TOOL_NAME = "reference.write"
RUN_ID = "r1"
SESSION_ID = "s1"


class ScriptedStream:
    """A stream_fn that answers with one message per request."""

    def __init__(self, *messages: AssistantMessage) -> None:
        if not messages:
            raise ValueError("a script needs at least one message")
        self.messages = list(messages)
        self.calls = 0

    async def __call__(self, model: Any, context: Any, options: Any = None) -> "ScriptedStream":
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self) -> "ScriptedStream":
        return self

    async def __anext__(self) -> Mapping[str, Any]:
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return self._final


def tool_call_message(term: str = "alpha") -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(id="call-write", name=TOOL_NAME, arguments={"term": term})],
        stop_reason=StopReason.TOOL_USE,
    )


def partial_message(names: Sequence[str]) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(id=f"c-{name}", name=name, arguments={"term": "alpha"}) for name in names
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def final_message(text: str = "done") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


def manifest(version: str = "1.0.0", names: Sequence[str] = (TOOL_NAME,)) -> ExtensionManifest:
    return ExtensionManifest(
        id=EXTENSION_ID,
        version=version,
        requires_capabilities=("tools.v1",),
        contributes=ContributionDeclarations(tools=tuple(names), skills=()),
    )


def binding(version: str = "1.0.0", names: Sequence[str] = (TOOL_NAME,)) -> ExtensionBinding:
    return ExtensionBinding(
        extension_id=EXTENSION_ID,
        namespace=NAMESPACE,
        authorization_scope=AUTHORIZATION_SCOPE,
        config_revision=CONFIG_REVISION,
        policy_revision=POLICY_REVISION,
        capabilities=frozenset({"tools.v1"}),
        tools=tuple(names),
        skills=(),
    )


def definition(
    effect_path: Path,
    *,
    name: str = TOOL_NAME,
    calls: list[str] | None = None,
    handler: Callable[..., Any] | None = None,
    dispatch_guard: Callable[[Mapping[str, Any]], str | None] | None = None,
) -> ToolDefinition:
    def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        term = arguments.get("term")
        if not isinstance(term, str) or not term.strip():
            raise ToolArgsInvalid("term must be a non-empty string")
        return {"term": term.strip()}

    async def counted(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        term = arguments["term"]
        if calls is not None:
            calls.append(term)
        with effect_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{context.identity.attempt_id}:{name}:{term}\n")
        return {"content": [TextContent(text=f"wrote {term}")], "details": {"term": term}}

    return ToolDefinition(
        name=name,
        description=f"Write one reference entry with {name}.",
        version="1",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=validate,
        handler=handler or counted,
        preview=lambda context, arguments: {"action": "append", "term": arguments["term"]},
        args_schema={
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
            "additionalProperties": False,
        },
        execution_mode=ExecutionMode.SEQUENTIAL,
        dispatch_guard=dispatch_guard,
    )


class _Factory:
    def __init__(self, tools: Sequence[ToolDefinition]) -> None:
        self.tools = tuple(tools)
        self.opened = 0
        self.closed = 0

    async def open(self, context: Any) -> ExtensionContribution:
        self.opened += 1
        return ExtensionContribution(tools=self.tools)

    async def close(self) -> None:
        self.closed += 1


@dataclass
class Bundle:
    root: Path
    deployment: Any
    harness: DefaultHarness
    gateway: ToolGateway
    approvals: SqliteApprovalStore
    backend: SqliteExecutionBackend
    persistence: SqlitePersistence
    context: RunContext
    stream: Any
    effect_path: Path
    calls: list[str] = field(default_factory=list)
    factory: Any = None

    def effect_lines(self) -> list[str]:
        if not self.effect_path.exists():
            return []
        return [
            line
            for line in self.effect_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def close(self) -> None:
        for store in (self.approvals, self.backend, self.persistence):
            store.close()


async def build(
    root: Path,
    *,
    version: str = "1.0.0",
    attempt: str = "a1",
    stream: Any | None = None,
    calls: list[str] | None = None,
    handler: Callable[..., Any] | None = None,
    effect_path: Path | None = None,
    limits: ExtensionLimits | None = None,
    context_labels: Mapping[str, Any] | None = None,
    close_stores: bool = False,
    auto_write: bool = False,
    dispatch_guard: Callable[[Mapping[str, Any]], str | None] | None = None,
    names: Sequence[str] = (TOOL_NAME,),
    handlers: Mapping[str, Callable[..., Any]] | None = None,
    reconcile: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    lease_ttl_seconds: float = 300.0,
    system_prompt: str = "be brief",
    model: Any = None,
    fingerprints: Mapping[str, str] | None = None,
) -> Bundle:
    """Build (and open) the fixed extension deployment in this process."""
    root.mkdir(parents=True, exist_ok=True)
    effect_path = effect_path or (root / "effects.log")
    tools = tuple(
        definition(
            effect_path,
            name=name,
            calls=calls,
            handler=(handlers or {}).get(name, handler),
            dispatch_guard=dispatch_guard,
        )
        for name in names
    )
    factory = _Factory(tools)
    plan = plan_extensions(
        manifests=[manifest(version, names)],
        factories={EXTENSION_ID: factory},
        bindings=[binding(version, names)],
        limits=limits or ExtensionLimits(),
        skill_limits=SkillLimits(),
    )
    deployment = await open_extensions(plan).open()
    approvals = SqliteApprovalStore(root / "approvals.db")
    backend = SqliteExecutionBackend(root / "operations.db")
    persistence = SqlitePersistence(root / "runs.db")
    gateway = ToolGateway(
        registry=deployment.registry,
        policy=ConservativePolicy(auto_write=auto_write),
        approvals=approvals,
        backend=backend,
    )
    context = RunContext(
        identity=RunIdentity(
            namespace=NAMESPACE,
            session_id=SESSION_ID,
            run_id=RUN_ID,
            attempt_id=attempt,
        ),
        principal=Principal(subject="author"),
        labels={
            "authorization_scope": AUTHORIZATION_SCOPE,
            **(dict(context_labels) if context_labels else {}),
        },
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: context)
    harness = deployment.bind_harness(
        stream_fn=stream or ScriptedStream(final_message()),
        gateway_bindings=bindings,
        persistence=persistence,
        system_prompt=system_prompt,
        model=model,
        fingerprints=fingerprints,
        reconcile=reconcile,
        clock=clock or time.time,
        lease_ttl_seconds=lease_ttl_seconds,
    )
    return Bundle(
        root=root,
        deployment=deployment,
        harness=harness,
        gateway=gateway,
        approvals=approvals,
        backend=backend,
        persistence=persistence,
        context=context,
        stream=stream,
        effect_path=effect_path,
        calls=calls if calls is not None else [],
        factory=factory,
    )
