"""0.6.0 P02：消息 codec 往返、自定义消息注册表与「未知类型不得继续跑」。

计划 §4.2 要求 core 事件名与 fixture 可比对，§90 要求自定义消息走**注册的**转换器、
未知类型保留可读并明确阻止不安全续跑。上游 `agent.ts:37-45` 的
`defaultConvertToLlm` 是把未知 role **静默过滤**掉 —— 那正是 §90 不接受的形状，所以这里
登记为差异并测：未知 role 必须显式拒绝，不能安静消失。
"""

import pytest

from yanxu_harness.core.messages import (
    CustomMessageRegistry,
    UnknownMessageRole,
    to_llm_messages,
)
from yanxu_harness.core.types import (
    AssistantMessage,
    StopReason,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
    message_from_json,
    message_to_json,
)


def test_round_trip_preserves_every_field_of_an_assistant_message() -> None:
    """往返必须逐字段等值 —— 「差不多能序列化」不算通过。"""
    original = AssistantMessage(
        content=[TextContent(text="正文"), ToolCall(id="c1", name="write_chapter", arguments={"path": "a.md"})],
        api="openai-completions",
        provider="deepseek",
        model="deepseek-chat",
        response_model="deepseek-chat-0528",
        response_id="resp-1",
        usage=Usage(input=None, output=7, total_tokens=None, reasoning=None),
        stop_reason=StopReason.TOOL_USE,
        error_message=None,
        raw_stop_reason="tool_calls",
        end_turn=False,
        timestamp=1700000000000,
    )
    assert message_from_json(message_to_json(original)) == original


def test_round_trip_keeps_unknown_usage_as_none_not_zero() -> None:
    """计划 §100：usage 缺失是未知，不是 0。上游 `EMPTY_USAGE` 填 0，属登记过的差异。"""
    original = AssistantMessage(usage=Usage())
    restored = message_from_json(message_to_json(original))
    assert restored.usage.input is None
    assert restored.usage.total_tokens is None
    assert restored.usage.reported is False


def test_round_trip_preserves_system_sections_and_tool_deltas() -> None:
    original = SystemMessage(
        content="基线",
        sections={"文风": "白描", "旧段": None},
        tools_added=[],
        tools_removed=[],
        timestamp=42,
    )
    restored = message_from_json(message_to_json(original))
    assert restored == original
    assert restored.sections == {"文风": "白描", "旧段": None}


def test_user_and_tool_result_round_trip() -> None:
    user = UserMessage(content=[TextContent(text="引用这段")], timestamp=7)
    assert message_from_json(message_to_json(user)) == user
    result = ToolResultMessage(
        tool_call_id="c1", tool_name="write_chapter", content=[TextContent(text="ok")], is_error=True, timestamp=8
    )
    assert message_from_json(message_to_json(result)) == result


def test_json_payload_is_plain_data_with_no_python_only_markers() -> None:
    import json

    payload = message_to_json(AssistantMessage(stop_reason=StopReason.ABORTED, error_message="断了"))
    assert json.loads(json.dumps(payload))["stop_reason"] == "aborted"


# --- 自定义消息 ---------------------------------------------------------------


def test_registered_custom_message_is_converted_by_its_own_converter() -> None:
    class FindingMessage:
        role = "finding"

        def __init__(self, text: str) -> None:
            self.text = text

    registry = CustomMessageRegistry()
    registry.register("finding", to_llm=lambda message: UserMessage(content=f"[发现] {message.text}"))

    converted = to_llm_messages([UserMessage(content="问"), FindingMessage("伏笔没收")], registry=registry)
    assert [m.content for m in converted] == ["问", "[发现] 伏笔没收"]


def test_ui_notifications_are_not_promoted_to_user_instructions() -> None:
    """计划 §90：UI 通知不得默认当成用户指令。注册表没登记就不进模型，且要显式报错。"""
    class Notice:
        role = "notification"

    with pytest.raises(UnknownMessageRole):
        to_llm_messages([Notice()], registry=CustomMessageRegistry())


def test_unknown_role_error_names_the_role_and_offers_no_silent_skip() -> None:
    class Alien:
        role = "alien"

    with pytest.raises(UnknownMessageRole) as excinfo:
        to_llm_messages([UserMessage(content="a"), Alien()], registry=CustomMessageRegistry())
    assert "alien" in str(excinfo.value)
    assert excinfo.value.index == 1, "要指得出是哪一条，否则宿主没法决定是丢历史还是阻塞续跑"


def test_registry_rejects_duplicate_and_reserved_roles() -> None:
    registry = CustomMessageRegistry()
    registry.register("finding", to_llm=lambda message: None)
    with pytest.raises(ValueError):
        registry.register("finding", to_llm=lambda message: None)
    with pytest.raises(ValueError):
        registry.register("user", to_llm=lambda message: None), "四种内建 role 不允许被覆盖"


def test_custom_message_round_trips_through_its_stored_form() -> None:
    """自定义消息必须能存回来再还原，否则 P09 迁移会把它们当垃圾丢掉。"""
    from dataclasses import dataclass

    @dataclass
    class Finding:
        role: str = "finding"
        text: str = ""

    registry = CustomMessageRegistry()
    registry.register(
        "finding",
        to_llm=lambda message: UserMessage(content=f"[发现] {message.text}"),
        to_stored=lambda message: {"role": "finding", "text": message.text},
        from_stored=lambda payload: Finding(text=payload["text"]),
    )
    stored = registry.store(Finding(text="伏笔没收"))
    assert registry.restore(stored) == Finding(text="伏笔没收")


def test_storage_converters_must_be_provided_as_a_pair() -> None:
    registry = CustomMessageRegistry()
    with pytest.raises(ValueError):
        registry.register("finding", to_llm=lambda m: None, to_stored=lambda m: {"role": "finding"})


def test_registered_role_that_is_not_restorable_is_refused_at_restore() -> None:
    registry = CustomMessageRegistry()
    registry.register("finding", to_llm=lambda m: None)
    with pytest.raises(UnknownMessageRole):
        registry.restore({"role": "finding", "text": "x"})
