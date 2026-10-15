"""0.6.0 P02：core 事件名与「原始 provider payload 不得外泄」。

上游 `agent/src/types.ts:448-463` 定义了 9 个事件与字段。计划 §4.2 要求首版**保留事件名与
语义**，所以这里逐个钉住名字和键；同时钉住 P01 契约范围 §4.2 找到的正面冲突：
`message_update` 携带 `assistantMessageEvent`（原始 provider 流事件），而 StoryIDE 的发布
不变量要求原始模型 payload 不进公开 DTO（计划 §218 第 3 条）。剥离点定在 P02，因为到了
P08 事件名已被 SSE 固定住，改不动。
"""

import pytest

from yanxu_harness.core.events import (
    AGENT_EVENT_NAMES,
    AgentEnd,
    AgentStart,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    TurnEnd,
    TurnStart,
    public_event_payload,
)
from yanxu_harness.core.types import AssistantMessage, TextContent


def test_event_names_match_upstream_verbatim() -> None:
    """计划 §4.2：首版保留上游事件名，改名要在宿主投影层做。"""
    assert AGENT_EVENT_NAMES == (
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


def test_agent_end_carries_the_new_messages_only() -> None:
    event = AgentEnd(messages=[AssistantMessage(content=[TextContent(text="hi")])])
    assert event.type == "agent_end"
    assert len(public_event_payload(event)["messages"]) == 1


def test_turn_end_exposes_message_and_tool_results() -> None:
    event = TurnEnd(message=AssistantMessage(), tool_results=[])
    payload = public_event_payload(event)
    assert set(payload) == {"type", "message", "tool_results"}


def test_tool_execution_events_carry_call_identity_and_error_flag() -> None:
    start = ToolExecutionStart(tool_call_id="call-1", tool_name="write_file", args={"path": "a.md"})
    update = ToolExecutionUpdate(tool_call_id="call-1", tool_name="write_file", args={}, partial_result={"text": "半截"})
    end = ToolExecutionEnd(tool_call_id="call-1", tool_name="write_file", result={"ok": True}, is_error=False)
    assert public_event_payload(start)["tool_call_id"] == "call-1"
    assert public_event_payload(update)["partial_result"] == {"text": "半截"}
    assert public_event_payload(end)["is_error"] is False


def test_message_update_rejects_raw_provider_event_in_public_payload() -> None:
    """核心不变量：投影到产品/浏览器的事件里不得出现原始 provider 事件。

    上游 `message_update` 的 `assistantMessageEvent` 是 provider 原生 delta；这里要求
    公开投影**必须**把它剔除，而不是靠调用方记得不读。
    """
    raw = {"type": "text_delta", "delta": " secret-ish provider frame", "partial": {"usage": {"input": 1234}}}
    update = MessageUpdate(message=AssistantMessage(content=[]), assistant_message_event=raw)
    payload = public_event_payload(update)
    assert "assistant_message_event" not in payload
    assert "provider_event" not in payload
    assert "secret-ish provider frame" not in repr(payload)


def test_public_payload_is_json_serialisable_without_provider_objects() -> None:
    import json

    from yanxu_harness.core.types import Usage

    message = AssistantMessage(content=[TextContent(text="一句")], usage=Usage(input=None, output=7), model="m")
    for event in (
        AgentStart(),
        MessageStart(message=message),
        MessageEnd(message=message),
        TurnStart(),
    ):
        json.dumps(public_event_payload(event), default=str)


def test_unknown_event_type_is_refused_not_silently_projected() -> None:
    class NotAnEvent:
        type = "made_up"

    with pytest.raises(TypeError):
        public_event_payload(NotAnEvent())
