"""0.6.0 P02：transcript 重放与工具声明差分。

逐行对照 `pi-ai@0.86.1` 的 `src/utils/transcript.ts` 与 `src/utils/text.ts`
（本地副本 `docs/reference/pi-0.86.1/upstream/packages/ai/src/utils/`）。

为什么这套东西必须在 core 里：计划 §4.1 要求「prompt/tools 基线可由 system transcript
重放」，而重放规则（后到的 content 追加、sections 按名覆盖、工具先 remove 后 add）一旦
在宿主里各写一遍就会分叉。`resolve_transcript()` 同时给出**不支持会话中途 system 消息**
的端点路径，那是 P01 Provider 能力清单 §4 点名的 hazard 的上游现成解。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .types import (
    AgentContext,
    SystemMessage,
    ToolDeclaration,
    ToolReference,
    ToolSpec,
    TranscriptContext,
)

__all__ = [
    "collapse_system_messages",
    "content_text",
    "create_initial_system_message",
    "declarations_equal",
    "get_current_system_message",
    "get_current_system_prompt",
    "get_current_tools",
    "get_declared_tools",
    "get_initial_system_message",
    "get_system_message_text",
    "get_tool_state_changes",
    "has_non_additive_tool_changes",
    "has_tool_redefinitions",
    "normalize_context",
    "render_system_message_update",
    "resolve_transcript",
    "resolve_transcript_tools",
    "to_tool_declaration",
    "without_initial_system_message",
    "ToolStateChanges",
    "TranscriptTools",
]

@dataclass(frozen=True)
class ToolStateChanges:
    tools_added: tuple[ToolDeclaration, ...] = ()
    tools_removed: tuple[ToolReference, ...] = ()


@dataclass(frozen=True)
class TranscriptTools:
    """`resolveTranscriptTools` 的返回：进请求顶层的工具集 + 能否按锚点增量加载。"""

    request_tools: tuple[Any, ...] = ()
    anchors_additions: bool = False


def content_text(content: str | Sequence[Any], separator: str = "\n") -> str:
    """text.ts:6-12：字符串原样，块数组只取 `type == "text"` 的部分再拼接。"""
    if isinstance(content, str):
        return content
    return separator.join(
        block.text for block in content if getattr(block, "type", None) == "text"
    )


def to_tool_declaration(tool: ToolSpec | ToolDeclaration) -> ToolDeclaration:
    """transcript.ts:123-130：只保留模型可见字段。

    上游用 `JSON.parse(JSON.stringify(parameters))` 做规范化以让结构比较精确；Python 侧
    用同样的 JSON 往返，把 `None` 字段与非 JSON 类型统一掉，否则宿主对象会让等值比较
    假阴。
    """
    return ToolDeclaration(
        name=tool.name,
        description=tool.description,
        parameters=json.loads(json.dumps(tool.parameters, default=_json_default)),
        constrained_sampling=tool.constrained_sampling,
    )


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):  # 宿主可能传 Pydantic 模型进来
        return value.model_dump(exclude_none=True)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def declarations_equal(left: Any, right: Any) -> bool:
    """transcript.ts:140-142：两边都先过 `to_tool_declaration` 再比序列化结果。"""
    return _canonical_json(to_tool_declaration(left)) == _canonical_json(to_tool_declaration(right))


def _canonical_json(declaration: ToolDeclaration) -> str:
    return json.dumps(
        {
            "name": declaration.name,
            "description": declaration.description,
            "parameters": declaration.parameters,
            **({} if declaration.constrained_sampling is None else {"constrained_sampling": declaration.constrained_sampling}),
        },
        sort_keys=True,
        default=_json_default,
    )


def create_initial_system_message(
    system_prompt: str | None,
    tools: Sequence[Any] | None,
) -> SystemMessage | None:
    """transcript.ts:10-23：两者都空时返回 None，空 transcript 保持空。"""
    has_prompt = system_prompt is not None and len(system_prompt) > 0
    has_tools = tools is not None and len(tools) > 0
    if not has_prompt and not has_tools:
        return None
    return SystemMessage(
        content=system_prompt if has_prompt else "",
        tools_added=[to_tool_declaration(t) for t in tools] if has_tools else None,
        timestamp=0,
    )


def normalize_context(context: AgentContext) -> TranscriptContext:
    """transcript.ts:30-34：唯一的 TranscriptContext 产出点。"""
    initial = create_initial_system_message(context.system_prompt, context.tools)
    messages = list(context.messages)
    if initial is not None:
        messages = [initial, *messages]
    return TranscriptContext(messages=tuple(messages))


def get_initial_system_message(messages: Iterable[Any]) -> SystemMessage | None:
    """transcript.ts:47-50：只有**开头**那条才算 initial。"""
    first = next(iter(messages), None)
    return first if isinstance(first, SystemMessage) else None


def without_initial_system_message(messages: Sequence[Any]) -> list[Any]:
    """transcript.ts:53-55：给「prompt 走请求顶层字段」的传输丢掉首条。"""
    return list(messages[1:]) if get_initial_system_message(messages) is not None else list(messages)


def get_current_tools(messages: Iterable[Any]) -> list[Any]:
    """transcript.ts:58-66：按顺序折叠 `tools_removed` 然后 `tools_added`。

    插入顺序由 dict 保序承担（等价于 JS `Map`），所以「删掉再加回来」会落到末尾。
    """
    tools: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, SystemMessage):
            continue
        for removed in message.tools_removed or []:
            tools.pop(removed.name, None)
        for added in message.tools_added or []:
            tools[added.name] = added
    return list(tools.values())


def get_current_system_message(messages: Iterable[Any]) -> SystemMessage | None:
    """transcript.ts:73-96：把每条 system 消息重放成一条当前状态。"""
    content: list[str] = []
    sections: dict[str, str] = {}
    timestamp: int | None = None
    saw_system_message = False
    for message in messages:
        if not isinstance(message, SystemMessage):
            continue
        saw_system_message = True
        if timestamp is None:
            timestamp = message.timestamp
        text = content_text(message.content)
        if text:
            content.append(text)
        for name, value in (message.sections or {}).items():
            if value is None:
                sections.pop(name, None)
            else:
                sections[name] = value
    tools = get_current_tools(messages)
    # transcript.ts:88 的守卫在 TS 里等价于「一条 system 消息都没有且没有工具」，
    # 因为 TS 的 timestamp 是必填。Python 侧没有类型强制，所以显式用 saw_system_message。
    if not saw_system_message and not tools:
        return None
    return SystemMessage(
        content="\n\n".join(content),
        sections=sections or None,
        tools_added=tools or None,
        timestamp=timestamp if timestamp is not None else 0,
    )


def get_system_message_text(message: SystemMessage) -> str:
    """text.ts:15-21：content 打头，sections 依次追加，空段丢弃。"""
    parts = [content_text(message.content)]
    parts.extend(text for text in (message.sections or {}).values() if text is not None)
    return "\n\n".join(part for part in parts if len(part) > 0)


def get_current_system_prompt(messages: Iterable[Any]) -> str:
    """transcript.ts:99-102。"""
    message = get_current_system_message(messages)
    return get_system_message_text(message) if message is not None else ""


def render_system_message_update(message: SystemMessage) -> str:
    """text.ts:28-40：section 被删除时要渲染成明示文本，不能静默消失。"""
    parts: list[str] = []
    text = content_text(message.content)
    if text:
        parts.append(text)
    for name, value in (message.sections or {}).items():
        parts.append(
            f'Removed system prompt section "{name}".'
            if value is None
            else f'Updated system prompt section "{name}":\n\n{value}'
        )
    return "\n\n".join(parts)


def collapse_system_messages(context: TranscriptContext) -> TranscriptContext:
    """transcript.ts:108-112：不支持会话中途 system 消息时的重建 —— 折进最前面一条，其余丢掉。"""
    head = get_current_system_message(context.messages)
    messages = [m for m in context.messages if not isinstance(m, SystemMessage)]
    if head is not None:
        messages = [head, *messages]
    return TranscriptContext(messages=tuple(messages))


def resolve_transcript(
    context: TranscriptContext,
    supports_mid_convo_system_messages: bool | None,
) -> TranscriptContext:
    """transcript.ts:115-120：能力未知按「不支持」处理（不猜厂商）。"""
    return context if supports_mid_convo_system_messages else collapse_system_messages(context)


def get_declared_tools(messages: Iterable[Any]) -> list[Any]:
    """transcript.ts:170-177：transcript 引用过的所有定义，按首次声明顺序。"""
    definitions: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, SystemMessage):
            continue
        for added in message.tools_added or []:
            definitions[added.name] = added
    return list(definitions.values())


def has_tool_redefinitions(messages: Iterable[Any]) -> bool:
    """transcript.ts:183-194：同名两次且定义不同。按名引用工具的传输表达不了这个。"""
    declared: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, SystemMessage):
            continue
        for added in message.tools_added or []:
            previous = declared.get(added.name)
            if previous is not None and not declarations_equal(previous, added):
                return True
            declared[added.name] = added
    return False


def has_non_additive_tool_changes(messages: Iterable[Any]) -> bool:
    """transcript.ts:197-208：出现过 remove 或同名重声明 → 只能整份重发工具集。"""
    declared: set[str] = set()
    for message in messages:
        if not isinstance(message, SystemMessage):
            continue
        if message.tools_removed:
            return True
        for added in message.tools_added or []:
            if added.name in declared:
                return True
            declared.add(added.name)
    return False


def resolve_transcript_tools(
    messages: Iterable[Any],
    supports_tool_additions: bool | None,
) -> TranscriptTools:
    """transcript.ts:226-234：能锚定增量才只发初始工具，否则发全量当前工具集。"""
    materialized = list(messages)
    anchors = bool(supports_tool_additions) and not has_non_additive_tool_changes(materialized)
    if anchors:
        initial = get_initial_system_message(materialized)
        request_tools = list(initial.tools_added or []) if initial is not None else []
    else:
        request_tools = get_current_tools(materialized)
    return TranscriptTools(request_tools=tuple(request_tools), anchors_additions=anchors)


def get_tool_state_changes(previous: Sequence[Any], current: Sequence[Any]) -> ToolStateChanges:
    """transcript.ts:150-167：改定义 = 一次 remove 加一次 add。"""
    previous_entries = []
    for tool in previous:
        declaration = to_tool_declaration(tool)
        previous_entries.append((tool, declaration, _canonical_json(declaration)))
    current_entries = []
    for tool in current:
        declaration = to_tool_declaration(tool)
        current_entries.append((tool, declaration, _canonical_json(declaration)))
    previous_map = {tool.name: (declaration, signature) for tool, declaration, signature in previous_entries}
    current_map = {tool.name: (declaration, signature) for tool, declaration, signature in current_entries}
    added = tuple(
        declaration
        for tool, declaration, signature in current_entries
        if tool.name not in previous_map or previous_map[tool.name][1] != signature
    )
    removed = tuple(
        ToolReference(tool.name)
        for tool, _declaration, signature in previous_entries
        if tool.name not in current_map or signature != current_map[tool.name][1]
    )
    return ToolStateChanges(tools_added=added, tools_removed=removed)


# --- 自定义消息：注册表与「未知 role 不得静默消失」 -----------------------------

LLM_ROLES = ("system", "user", "assistant", "toolResult")


class UnknownMessageRole(Exception):
    """transcript 里出现了没登记的 role。

    上游 `agent.ts:37-45` 的 `defaultConvertToLlm` 把未知 role **直接过滤掉**。StoryIDE 不
    接受这个形状（计划 §90）：静默丢弃会让「模型看到的上下文」和「作者看到的对话」悄悄分叉，
    而且历史里留着这条消息、下一次读回来又不认识 —— 所以这里显式拒绝，并带上下标让宿主能
    决定是丢历史还是阻塞续跑。这是登记过的行为差异。
    """

    def __init__(self, role: str, index: int) -> None:
        super().__init__(f"第 {index} 条消息的 role {role!r} 没有注册转换器，拒绝把它当可继续的上下文")
        self.role = role
        self.index = index


class CustomMessageRegistry:
    """role → 转换器。TS 靠 declaration merging 扩展，Python 必须显式登记。"""

    def __init__(self) -> None:
        self._to_llm: dict[str, Callable[[Any], Any]] = {}
        self._to_stored: dict[str, Callable[[Any], dict[str, Any]]] = {}
        self._from_stored: dict[str, Callable[[Mapping[str, Any]], Any]] = {}

    def register(
        self,
        role: str,
        to_llm: Callable[[Any], Any],
        *,
        to_stored: Callable[[Any], dict[str, Any]] | None = None,
        from_stored: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        if role in LLM_ROLES:
            raise ValueError(f"内建 role {role!r} 不允许被覆盖")
        if not callable(to_llm):
            raise TypeError("to_llm 必须是可调用对象")
        if role in self._to_llm:
            raise ValueError(f"role {role!r} 已注册；覆盖注册要显式 unregister")
        if (to_stored is None) != (from_stored is None):
            raise ValueError("to_stored 与 from_stored 必须成对提供，否则历史读回来无法还原")
        self._to_llm[role] = to_llm
        if to_stored is not None and from_stored is not None:
            self._to_stored[role] = to_stored
            self._from_stored[role] = from_stored

    def unregister(self, role: str) -> None:
        for table in (self._to_llm, self._to_stored, self._from_stored):
            table.pop(role, None)

    def knows(self, role: str) -> bool:
        return role in self._to_llm

    def to_llm(self, message: Any) -> Any:
        role = getattr(message, "role", None)
        if role not in self._to_llm:
            raise UnknownMessageRole(str(role), -1)
        return self._to_llm[role](message)

    def store(self, message: Any) -> dict[str, Any]:
        role = getattr(message, "role", None)
        if role not in self._to_stored:
            raise UnknownMessageRole(str(role), -1)
        return self._to_stored[role](message)

    def restore(self, payload: Mapping[str, Any]) -> Any:
        role = payload.get("role")
        if role not in self._from_stored:
            raise UnknownMessageRole(str(role), -1)
        return self._from_stored[role](payload)


def to_llm_messages(
    messages: Sequence[Any],
    *,
    registry: CustomMessageRegistry | None = None,
) -> list[Any]:
    """把 transcript 变成模型可见消息；未注册的自定义 role 一律抛 `UnknownMessageRole`。"""
    active = registry or CustomMessageRegistry()
    out: list[Any] = []
    for index, message in enumerate(messages):
        role = getattr(message, "role", None)
        if role in LLM_ROLES:
            out.append(message)
            continue
        if active.knows(str(role)):
            out.append(active.to_llm(message))
            continue
        raise UnknownMessageRole(str(role), index)
    return out
