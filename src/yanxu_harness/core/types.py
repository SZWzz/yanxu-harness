"""0.6.0 P02：core 的通用消息、工具与能力契约。

对照 `@earendil-works/pi-agent-core` 0.86.1（commit `c7cdb46`）与 `pi-ai` 同 commit 的
`src/types.ts`；归属与许可证见 `THIRD_PARTY_NOTICES.md`。上游副本不在本仓库里（它既不是
构建依赖也不是测试依赖），所以文件内引用的是包名 + commit，而不是本地路径。移植规则：
字段一一对应，命名转 snake_case；TS 的 `undefined` 转 `None`；行为差异必须在注释里写明并配
测试。

core 不知道任何 Web 框架、应用存储、厂商客户端或宿主配置 —— 由
`tests/test_import_fence.py` 机械固定。包声明是 `>=3.10`，所以这里不用 3.11+ 独占 API
（同一守卫）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence

__all__ = [
    "STOP_REASONS",
    "AgentContext",
    "AssistantMessage",
    "DeferredHandle",
    "ImageContent",
    "Model",
    "StopReason",
    "SystemMessage",
    "TextContent",
    "ThinkingContent",
    "ToolCall",
    "ToolDeclaration",
    "ToolReference",
    "ToolSpec",
    "TranscriptContext",
    "Usage",
    "UserMessage",
    "block_from_json",
    "block_to_json",
    "declaration_from_json",
    "declaration_to_json",
    "message_from_json",
    "message_to_json",
    "now_ms",
    "usage_from_json",
    "usage_to_json",
]


def now_ms() -> int:
    """上游把 `timestamp` 声明为必填的 Unix 毫秒；Python 侧给个默认值以免每个构造点重复。"""
    return int(time.time() * 1000)


# types.ts:StopReason 的全部七个值。`deferred` 与 `pending` 容易被漏掉：`deferred` 是
# 上游给「回合挂起等待外部结果」用的终态，正是 StoryIDE 持久审批要对齐的位置（计划 §5.1）。
STOP_REASONS = ("pending", "stop", "length", "toolUse", "error", "aborted", "deferred")


class StopReason(str, Enum):
    PENDING = "pending"
    STOP = "stop"
    LENGTH = "length"
    TOOL_USE = "toolUse"
    ERROR = "error"
    ABORTED = "aborted"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class TextContent:
    type: str = "text"
    text: str = ""
    text_signature: str | None = None


@dataclass(frozen=True)
class ThinkingContent:
    """模型提供的思考文本可在有界产品事件中展示；厂商签名仍仅供内部续传。"""

    type: str = "thinking"
    thinking: str = ""
    thinking_signature: str | None = None
    redacted: bool | None = None


@dataclass(frozen=True)
class ImageContent:
    type: str = "image"
    data: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class ToolCall:
    type: str = "toolCall"
    id: str = ""
    name: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    thought_signature: str | None = None
    namespace: str | None = None


@dataclass(frozen=True)
class Usage:
    """上游的 input/output/totalTokens 是必填 number，但计划 §100 要求「缺失记为未知、不得伪造 0」。

    所以 Python 侧全部允许 `None`：`None` = 厂商没报，`0` = 厂商报了 0。两者在计费与
    上下文预算上是不同的事实，不能合并。
    """

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    cache_write_1h: int | None = None
    reasoning: int | None = None
    total_tokens: int | None = None
    cost: Mapping[str, Any] | None = None

    @property
    def reported(self) -> bool:
        return any(v is not None for v in (self.input, self.output, self.total_tokens))


@dataclass(frozen=True)
class DeferredHandle:
    """上游 `AssistantMessage.deferred` 的最小表示：只带身份与宿主给的载荷。

    真正的暂停/恢复协议在 `backend/agent_runtime/`（计划 §5.1 的宿主结果），core 只保证
    这个字段能穿过 transcript 不丢。
    """

    id: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class SystemMessage:
    role: str = "system"
    content: str | Sequence[TextContent] = ""
    sections: dict[str, str | None] | None = None
    tools_added: list["ToolDeclaration"] | None = None
    tools_removed: list["ToolReference"] | None = None
    timestamp: int = field(default_factory=now_ms)


@dataclass
class UserMessage:
    role: str = "user"
    content: str | Sequence[Any] = ""
    timestamp: int = field(default_factory=now_ms)


@dataclass
class AssistantMessage:
    role: str = "assistant"
    content: list[Any] = field(default_factory=list)
    api: str | None = None
    provider: str | None = None
    model: str | None = None
    response_model: str | None = None
    response_id: str | None = None
    provider_thinking_level: str | None = None
    usage: Usage = field(default_factory=Usage)
    stop_reason: StopReason = StopReason.PENDING
    deferred: DeferredHandle | None = None
    error_message: str | None = None
    raw_stop_reason: str | None = None
    end_turn: bool | None = None
    timestamp: int = field(default_factory=now_ms)


@dataclass
class ToolResultMessage:
    role: str = "toolResult"
    tool_call_id: str = ""
    tool_name: str = ""
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False
    usage: Usage | None = None
    timestamp: int = field(default_factory=now_ms)
    # StoryIDE host-only history metadata. Provider adapters must not serialize it.
    source_provenance: Any = None


Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True)
class ToolReference:
    name: str


@dataclass(frozen=True)
class ToolDeclaration:
    """模型可见的工具接口。

    `to_tool_declaration()` 特意产出这个**独立类型**而不是复用 `ToolSpec`：上游比较的
    正是「模型看到的东西变没变」（transcript.ts:123-130），把 label/执行模式/replay 等
    宿主字段带进比较会让每次换实现都误报工具集变更。
    """

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    constrained_sampling: Mapping[str, Any] | bool | None = None


@dataclass
class ToolSpec:
    """可执行工具 = 模型可见声明 + 宿主执行信息（types.ts:406-431）。"""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    label: str = ""
    constrained_sampling: Mapping[str, Any] | bool | None = None
    # (tool_call_id, args, signal, on_update) -> AgentToolResult；抛异常即失败（types.ts:414）
    execute: Callable[..., Any] | None = None
    prepare_arguments: Callable[[Any], Any] | None = None
    execution_mode: str | None = None  # "sequential" | "parallel"；None = 跟随批次默认
    # types.ts:422 的恢复策略：durable intent 已存在但结果未知时能不能重放。
    # 默认 "never" —— 未知就不重放，这是 StoryIDE `outcome_unknown` 的保守落点。
    replay: str = "never"
    #: Optional recovery capability of the adapter behind this tool, populated by
    #: ``yanxu_harness.tools.core_bridge``. Plain data the driver uses to ask the
    #: authoritative ledger about one call *without* running it; ``None`` means this tool
    #: cannot be asked and its calls must not be recovered automatically.
    recovery: Any = None
    #: Optional pre-dispatch guard copied from the tool definition. The harness runs it
    #: before resuming a paused batch; a non-empty reason refuses the resume before any
    #: call is dispatched or any model request is made.
    dispatch_guard: Any = None


@dataclass
class AgentToolResult:
    """工具产出（types.ts:383-395）。`content=None` 一律归一成空列表，不让 None 进历史。"""

    content: list[Any] = field(default_factory=list)
    details: Any = None
    usage: Usage | None = None
    terminate: bool | None = None


@dataclass
class AgentContext:
    """一次运行的输入：prompt/tools 简写 + 已有消息（types.ts Context）。"""

    messages: list[Any] = field(default_factory=list)
    system_prompt: str | None = None
    tools: list[ToolSpec] | None = None

    def to_transcript(self) -> "TranscriptContext":
        from .messages import normalize_context

        return normalize_context(self)


@dataclass(frozen=True)
class TranscriptContext:
    """`normalize_context()` 的唯一产出类型；Provider Adapter 只能接这个。

    上游用一个 `unique symbol` brand 防绕过（types.ts:626），Python 侧靠「只有
    `normalize_context` 构造它」这条约定 + 测试。
    """

    messages: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Model:
    """core 看到的模型句柄：身份 + 窗口 + 能力开关，不含凭据。

    两个 `supports_*` 默认 **False**（P01 Provider 能力清单 §4）：探测不到就按不支持，
    否则会把工具声明变化用端点不接受的形状发出去，失败成 400 或静默丢弃。
    """

    id: str
    provider: str = ""
    api: str = ""
    base_url: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning: bool = False
    input: tuple[str, ...] = ("text",)
    supports_mid_convo_system_messages: bool = False
    supports_mid_convo_tool_additions: bool = False


class StreamFn(Protocol):
    """一次模型请求 + 流解析的注入点（types.ts:28-37）。

    契约三条：不得为请求/模型/运行时失败抛异常；必须返回事件流；失败要编码进终态事件
    （`stop_reason ∈ {error, aborted}` + `error_message`）。
    """

    def __call__(
        self,
        model: Model,
        context: TranscriptContext,
        options: Mapping[str, Any] | None = None,
    ) -> Any: ...


# --- JSON 往返：core 与 fixture / 宿主存储之间的 plain data ---------------------

_BLOCK_FIELDS = {
    "text": ("text", "text_signature"),
    "thinking": ("thinking", "thinking_signature", "redacted"),
    "image": ("data", "mime_type"),
    "toolCall": ("id", "name", "arguments", "thought_signature", "namespace"),
}


def block_to_json(block: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": block.type}
    for name in _BLOCK_FIELDS[block.type]:
        value = getattr(block, name, None)
        if value is not None:
            payload[name] = value
    return payload


def block_from_json(payload: Mapping[str, Any]) -> Any:
    kind = payload.get("type")
    builders = {"text": TextContent, "thinking": ThinkingContent, "image": ImageContent, "toolCall": ToolCall}
    if kind not in builders:
        raise ValueError(f"未知 content block 类型 {kind!r}")
    cls = builders[kind]
    accepted = {name: value for name, value in payload.items() if name in cls.__dataclass_fields__ and name != "type"}
    return cls(type=kind, **accepted)


def usage_to_json(usage: Usage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        # A host hook may patch `usage` with a plain mapping (`after_tool_call` does);
        # the loop accepts that shape, so the codec must not be stricter than the loop.
        return {str(key): value for key, value in usage.items()}
    return {name: getattr(usage, name) for name in usage.__dataclass_fields__}


def usage_from_json(payload: Mapping[str, Any] | None) -> Usage | None:
    if payload is None:
        return None
    return Usage(**{k: v for k, v in payload.items() if k in Usage.__dataclass_fields__})


def declaration_to_json(tool: ToolDeclaration) -> dict[str, Any]:
    return {name: getattr(tool, name) for name in tool.__dataclass_fields__}


def declaration_from_json(payload: Mapping[str, Any]) -> ToolDeclaration:
    return ToolDeclaration(**{k: v for k, v in payload.items() if k in ToolDeclaration.__dataclass_fields__})


def message_to_json(message: Any) -> dict[str, Any]:
    """四种内建 role → plain dict。None 一律保留为 null：区分「没报」与「报了 0」。"""
    role = getattr(message, "role", None)
    if role == "system":
        content = message.content
        return {
            "role": "system",
            "content": content if isinstance(content, str) else [block_to_json(b) for b in content],
            "sections": message.sections,
            "tools_added": None if message.tools_added is None else [declaration_to_json(t) for t in message.tools_added],
            "tools_removed": None if message.tools_removed is None else [{"name": t.name} for t in message.tools_removed],
            "timestamp": message.timestamp,
        }
    if role == "user":
        content = message.content
        return {
            "role": "user",
            "content": content if isinstance(content, str) else [block_to_json(b) for b in content],
            "timestamp": message.timestamp,
        }
    if role == "assistant":
        return {
            "role": "assistant",
            "content": [block_to_json(b) for b in message.content],
            "api": message.api,
            "provider": message.provider,
            "model": message.model,
            "response_model": message.response_model,
            "response_id": message.response_id,
            "provider_thinking_level": message.provider_thinking_level,
            "usage": usage_to_json(message.usage),
            "stop_reason": message.stop_reason.value if isinstance(message.stop_reason, StopReason) else message.stop_reason,
            "deferred": None if message.deferred is None else {"id": message.deferred.id, "payload": dict(message.deferred.payload)},
            "error_message": message.error_message,
            "raw_stop_reason": message.raw_stop_reason,
            "end_turn": message.end_turn,
            "timestamp": message.timestamp,
        }
    if role == "toolResult":
        payload = {
            "role": "toolResult",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": [block_to_json(b) for b in message.content],
            "details": message.details,
            "is_error": message.is_error,
            "usage": usage_to_json(message.usage),
            "timestamp": message.timestamp,
        }
        if message.source_provenance is not None:
            payload["storyide_source_provenance"] = message.source_provenance
        return payload
    raise ValueError(f"未知 message role {role!r}；自定义消息要走 CustomMessageRegistry，不进 core codec")


def message_from_json(payload: Mapping[str, Any]) -> Any:
    role = payload.get("role")
    if role == "system":
        content = payload.get("content") or ""
        removed = payload.get("tools_removed")
        added = payload.get("tools_added")
        return SystemMessage(
            content=content,
            sections=payload.get("sections"),
            tools_added=None if added is None else [declaration_from_json(t) for t in added],
            tools_removed=None if removed is None else [ToolReference(t["name"]) for t in removed],
            timestamp=payload.get("timestamp", 0),
        )
    if role == "user":
        content = payload.get("content") or ""
        return UserMessage(
            content=content if isinstance(content, str) else [block_from_json(b) for b in content],
            timestamp=payload.get("timestamp", 0),
        )
    if role == "assistant":
        deferred = payload.get("deferred")
        stop_reason = payload.get("stop_reason", StopReason.PENDING.value)
        return AssistantMessage(
            content=[block_from_json(b) for b in payload.get("content") or []],
            api=payload.get("api"),
            provider=payload.get("provider"),
            model=payload.get("model"),
            response_model=payload.get("response_model"),
            response_id=payload.get("response_id"),
            provider_thinking_level=payload.get("provider_thinking_level"),
            usage=usage_from_json(payload.get("usage")) or Usage(),
            stop_reason=StopReason(stop_reason) if stop_reason in STOP_REASONS else StopReason.PENDING,
            deferred=None if deferred is None else DeferredHandle(id=deferred["id"], payload=deferred.get("payload") or {}),
            error_message=payload.get("error_message"),
            raw_stop_reason=payload.get("raw_stop_reason"),
            end_turn=payload.get("end_turn"),
            timestamp=payload.get("timestamp", 0),
        )
    if role == "toolResult":
        return ToolResultMessage(
            tool_call_id=payload.get("tool_call_id", ""),
            tool_name=payload.get("tool_name", ""),
            content=[block_from_json(b) for b in payload.get("content") or []],
            details=payload.get("details"),
            is_error=bool(payload.get("is_error", False)),
            usage=usage_from_json(payload.get("usage")),
            timestamp=payload.get("timestamp", 0),
            source_provenance=payload.get("storyide_source_provenance"),
        )
    raise ValueError(f"未知 message role {role!r}")
