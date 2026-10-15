"""0.6.0 P02：core 事件与「面向产品」的投影。

事件名与字段逐个对照 `agent/src/types.ts:448-463`（共 **10** 个），计划 §4.2 要求首版保留
上游名称与语义，改名只允许发生在 `backend/agent_runtime/event_projection.py`。

这里同时固定 P01 契约范围 §4.2 找到的冲突解法：`message_update` 在 core 内**可以**携带
provider 原生事件（对齐上游），但 `public_event_payload()` 是宿主唯一的对外出口，它
**结构上不给**那个字段留通道。理由：如果只靠调用方「记得不读」，任何一处新增投影就会把
原始 provider payload 泄漏给浏览器，违反计划 §218 第 3 条。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "AGENT_EVENT_NAMES",
    "AgentEnd",
    "AgentEvent",
    "AgentStart",
    "MessageEnd",
    "MessageStart",
    "MessageUpdate",
    "ToolExecutionEnd",
    "ToolExecutionStart",
    "ToolExecutionUpdate",
    "TurnEnd",
    "TurnStart",
    "public_event_payload",
]


@dataclass(frozen=True)
class AgentStart:
    type: str = field(default="agent_start", init=False)


@dataclass(frozen=True)
class AgentEnd:
    messages: tuple[Any, ...] = ()
    type: str = field(default="agent_end", init=False)


@dataclass(frozen=True)
class TurnStart:
    type: str = field(default="turn_start", init=False)


@dataclass(frozen=True)
class TurnEnd:
    message: Any = None
    tool_results: tuple[Any, ...] = ()
    type: str = field(default="turn_end", init=False)


@dataclass(frozen=True)
class MessageStart:
    message: Any = None
    type: str = field(default="message_start", init=False)


@dataclass(frozen=True)
class MessageUpdate:
    """`assistant_message_event` 保持 provider 私有；宿主只可单独投影允许展示的归一化字段。"""

    message: Any = None
    assistant_message_event: Any = None
    type: str = field(default="message_update", init=False)


@dataclass(frozen=True)
class MessageEnd:
    message: Any = None
    type: str = field(default="message_end", init=False)


@dataclass(frozen=True)
class ToolExecutionStart:
    tool_call_id: str
    tool_name: str
    args: Any = None
    type: str = field(default="tool_execution_start", init=False)


@dataclass(frozen=True)
class ToolExecutionUpdate:
    tool_call_id: str
    tool_name: str
    args: Any = None
    partial_result: Any = None
    type: str = field(default="tool_execution_update", init=False)


@dataclass(frozen=True)
class ToolExecutionEnd:
    tool_call_id: str
    tool_name: str
    result: Any = None
    is_error: bool = False
    type: str = field(default="tool_execution_end", init=False)


AgentEvent = (
    AgentStart
    | AgentEnd
    | TurnStart
    | TurnEnd
    | MessageStart
    | MessageUpdate
    | MessageEnd
    | ToolExecutionStart
    | ToolExecutionUpdate
    | ToolExecutionEnd
)

# 顺序即上游 union 的声明顺序。P08 的事件投影按这张表映射。
AGENT_EVENT_NAMES: tuple[str, ...] = (
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
)

# 每个事件里不得外泄的字段。目前是 MessageUpdate 的 provider 原生帧。
PRIVATE_FIELDS: dict[str, tuple[str, ...]] = {"message_update": ("assistant_message_event",)}

# 只用于「继续对话」的字段，不进任何公开 payload：厂商签名是续传材料，不是展示数据，
# 而 `yanxu_harness.events.assert_public_payload` 连裸键 `signature` 都拒绝。在唯一对外
# 出口里按字段名剔除，就不需要每个调用方记得自己的消息里有没有带签名。
PRIVATE_CONTINUATION_FIELDS: frozenset[str] = frozenset(
    {"text_signature", "thinking_signature", "thought_signature"}
)


def public_event_payload(event: Any) -> dict[str, Any]:
    """宿主唯一允许的对外投影：剥掉 provider 专有字段，其余原样转成 plain data。

    从 dataclass 字段直接构造，而不是先 `asdict` 再删键：`asdict` 会把嵌套的
    dataclass 一次性展开，之后就没有「哪个字段是续传材料」的信息了。
    """
    event_type = getattr(event, "type", None)
    if event_type not in AGENT_EVENT_NAMES:
        raise TypeError(f"不是 core 事件（type={event_type!r}），拒绝投影而不是静默产出空 payload")
    private = PRIVATE_FIELDS.get(event_type, ())
    return {
        name: _plain(getattr(event, name))
        for name in (field.name for field in dataclasses.fields(event))
        if name not in private
    }


def _plain(value: Any) -> Any:
    """把 dataclass / Enum / Mapping / tuple 递归转成 JSON 可写的 plain data。

    递归也按字段名剔除续传材料（见 `PRIVATE_CONTINUATION_FIELDS`），所以消息里带的厂商签名
    不会因为「它藏在 message.content 里」而漏出去。
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.name not in PRIVATE_CONTINUATION_FIELDS
        }
    if isinstance(value, Mapping):
        # Mapping *keys* are filtered too: a host-supplied `details` payload is a plain
        # dict, so a continuation field hidden inside one would otherwise reach a public
        # payload even though the same name on a dataclass is stripped. Those names are
        # reserved for private recovery material; they do not appear in public events.
        return {
            str(k): _plain(v)
            for k, v in value.items()
            if str(k) not in PRIVATE_CONTINUATION_FIELDS
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value
