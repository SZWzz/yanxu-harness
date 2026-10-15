"""Shared helpers for the context tests.

The call/result contract (`assert_no_orphan_tool_results`) and the *real* conversion used
for measurement live here, so the projection tests, the boundary tests and the C3 wiring
tests assert the same contract against the same conversion instead of three slightly
different copies of it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from yanxu_harness.context import MeasurementInput
from yanxu_harness.core.messages import normalize_context, to_llm_messages
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolDeclaration,
    ToolReference,
    ToolResultMessage,
    UserMessage,
)

__all__ = [
    "assert_no_orphan_tool_results",
    "assistant_call",
    "assistant_text",
    "declaration",
    "real_convert",
    "system",
    "tool_result",
    "user",
]


def declaration(
    name: str, *, description: str = "", parameters: Mapping[str, Any] | None = None
) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        description=description or name,
        parameters=dict(parameters or {"type": "object", "properties": {}}),
    )


def system(
    content: str = "",
    *,
    sections: Mapping[str, str] | None = None,
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
) -> SystemMessage:
    return SystemMessage(
        content=content,
        sections=dict(sections) if sections else None,
        tools_added=[declaration(name) for name in added] or None,
        tools_removed=[ToolReference(name) for name in removed] or None,
    )


def user(text: str) -> UserMessage:
    return UserMessage(content=text)


def assistant_text(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


def assistant_call(
    call_id: str, name: str, *, text: str = "", extra: Sequence[tuple[str, str]] = ()
) -> AssistantMessage:
    """An assistant turn that declares tool calls (the fragment a result must stay with)."""
    calls = [ToolCall(id=call_id, name=name, arguments={"path": "a.md"})]
    calls.extend(ToolCall(id=other_id, name=other_name, arguments={}) for other_id, other_name in extra)
    blocks = ([TextContent(text=text)] if text else []) + calls
    return AssistantMessage(content=blocks, stop_reason=StopReason.TOOL_USE)


def tool_result(call_id: str, text: str, *, name: str = "read", is_error: bool = False) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(text=text)],
        is_error=is_error,
    )


def real_convert(messages: Sequence[Any]) -> MeasurementInput:
    """The real conversion: transcript → LLM messages → the provider request shape.

    This is the same `to_provider_request` the wire path uses, so a projection measured
    through it is measured against the request that would actually be sent (system folding
    and tool declarations included), not against a private re-telling of it.
    """
    from yanxu_harness.providers.core_bridge import to_provider_request

    llm = to_llm_messages(list(messages))
    request = to_provider_request(None, normalize_context(AgentContext(messages=llm)))
    return MeasurementInput.from_request(request)


def assert_no_orphan_tool_results(
    messages: Sequence[Mapping[str, Any]], *, require_results: bool = True
) -> None:
    """Every call/result pair of a request view must be closed inside that view.

    This is section 3.2 of the plan, and it is a *harness* acceptance contract rather than
    a claim about what a provider returns for a broken transcript: a result without its
    declaring assistant message, or a declared call whose result was dropped, is not a
    smaller valid request — it is a different, invalid one. ``require_results=False`` is
    for the one legitimate exception: a batch awaiting an external decision never becomes
    a model request at all.
    """
    declared: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                declared.add(call["id"])
        elif message.get("role") in ("toolResult", "tool"):
            call_id = message.get("tool_call_id")
            assert call_id in declared, (
                f"toolResult {call_id!r} has no declaring assistant message in this view: "
                "the call and its result were split"
            )
    if require_results:
        answered = {
            message.get("tool_call_id")
            for message in messages
            if message.get("role") in ("toolResult", "tool")
        }
        for call_id in sorted(declared):
            assert call_id in answered, (
                f"call {call_id!r} was declared in this view but its result is missing"
            )
