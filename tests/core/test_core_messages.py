"""0.6.0 P02：消息与 system/tool transcript 重放（对照移植，非自创）。

每条断言都对着上游 pi-ai 0.86.1（commit `c7cdb46`）的 `src/utils/transcript.ts`
与 `.../text.ts` 在 commit `c7cdb46` 的行为写的，逐条注明行号。差异只在 Python 习惯
（snake_case、`None` 代替 `undefined`），行为差异必须在此文件登记。

计划 §4.1/§4.2 要的三件事在这里被固定：prompt/tools 基线可由 system transcript 重放；
工具集变化以「会话中途 system 消息」宣告；不支持该形状的端点走 collapse 路径（§5.1 与
P01 Provider 能力清单 §4 的 hazard 的上游现成解）。
"""

import pytest

from yanxu_harness.core import messages as messages_module
from yanxu_harness.core.messages import (
    collapse_system_messages,
    create_initial_system_message,
    declarations_equal,
    get_current_system_message,
    get_current_system_prompt,
    get_current_tools,
    get_tool_state_changes,
    has_non_additive_tool_changes,
    has_tool_redefinitions,
    normalize_context,
    render_system_message_update,
    resolve_transcript,
    resolve_transcript_tools,
    to_tool_declaration,
)
from yanxu_harness.core.types import AgentContext, SystemMessage, ToolSpec


def tool(name: str, schema: dict | None = None, **extra) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} 的说明",
        parameters=schema if schema is not None else {"type": "object", "properties": {}},
        **extra,
    )


# --- 种入与归一 ---------------------------------------------------------------


def test_empty_prompt_and_no_tools_keeps_the_transcript_empty() -> None:
    """transcript.ts:16 两侧都空时返回 undefined，所以空的 transcript 不会多一条 system 消息。"""
    assert create_initial_system_message(None, None) is None
    assert create_initial_system_message("", []) is None
    context = AgentContext(messages=[], system_prompt="", tools=[])
    assert normalize_context(context).messages == (), "只读视图用 tuple，宿主不能就地改 transcript"


def test_normalize_context_folds_prompt_and_tools_into_a_leading_system_message() -> None:
    """transcript.ts:30-34：唯一的 TranscriptContext 产出点。"""
    context = AgentContext(messages=[], system_prompt="小说作者助手", tools=[tool("write_chapter")])
    transcript = normalize_context(context)
    first = transcript.messages[0]
    assert isinstance(first, SystemMessage)
    assert first.content == "小说作者助手"
    assert [t.name for t in first.tools_added] == ["write_chapter"]


def test_tools_only_system_message_keeps_content_empty() -> None:
    """transcript.ts:19 `content: systemPrompt ?? ""` —— 只有工具时内容是空串而不是 None。"""
    message = create_initial_system_message(None, [tool("search")])
    assert message is not None and message.content == ""


# --- 重放：prompt 与 sections ------------------------------------------------


def test_replay_joins_later_system_content_and_patches_sections_by_name() -> None:
    """transcript.ts:73-96：content 用 \\n\\n 串接，sections 按名覆盖、None 表示删除。"""
    messages = [
        SystemMessage(content="基线", sections={"风格": "第一人称", "字数": "500"}),
        SystemMessage(content="追加", sections={"风格": None}),
        SystemMessage(sections={"字数": "1200"}),
    ]
    replayed = get_current_system_message(messages)
    assert replayed is not None
    assert replayed.content == "基线\n\n追加"
    assert replayed.sections == {"字数": "1200"}


def test_replay_timestamp_is_the_first_system_message_timestamp() -> None:
    """transcript.ts:79 `timestamp ??=`：后续 system 消息的时间戳不参与。"""
    messages = [SystemMessage(content="a", timestamp=111), SystemMessage(content="b", timestamp=999)]
    assert get_current_system_message(messages).timestamp == 111


def test_replay_returns_none_when_nothing_was_ever_declared() -> None:
    """transcript.ts:88 既没有 system 消息也没有工具 → None（不是空对象）。"""
    assert get_current_system_message([]) is None
    assert get_current_system_prompt([]) == ""


def test_system_prompt_text_includes_content_then_sections() -> None:
    """text.ts:15-20：content 打头，sections 依次追加，空段被滤掉。"""
    messages = [SystemMessage(content="基线", sections={"文风": "克制"})]
    assert get_current_system_prompt(messages) == "基线\n\n克制"


# --- 重放：工具集增删 --------------------------------------------------------


def test_tool_deltas_fold_in_declaration_order_and_removals_win() -> None:
    """transcript.ts:58-66：按 system 消息顺序折叠，先 remove 后 add，最后落定。"""
    t1, t2 = tool("a"), tool("b")
    messages = [
        SystemMessage(tools_added=[t1, t2]),
        SystemMessage(tools_removed=[t1]),
        SystemMessage(tools_added=[tool("a")]),
    ]
    assert [t.name for t in get_current_tools(messages)] == ["b", "a"]


def test_within_one_message_removal_is_applied_before_addition() -> None:
    """上游在同一批里也固定「先 remove 后 add」（transcript.ts:62-63），所以自替换一条消息 = 该工具在场。

    上一版的跨消息场景对「顺序反过来」是等价的（add 后再 remove 也得到 ["b","a"]），
    变异因此存活；这条才是能区分两种实现的最小形状。
    """
    messages = [SystemMessage(tools_added=[tool("a")], tools_removed=[tool("a")])]
    assert [t.name for t in get_current_tools(messages)] == ["a"]


def test_removal_after_addition_actually_removes() -> None:
    messages = [SystemMessage(tools_added=[tool("a")]), SystemMessage(tools_removed=[tool("a")])]
    assert get_current_tools(messages) == []


def test_changed_definition_counts_as_removal_plus_addition() -> None:
    """transcript.ts:150-167 同名片段改定义 = 先 remove 再 add。"""
    changes = get_tool_state_changes([tool("a", {"type": "object"})], [tool("a", {"type": "object", "additionalProperties": False})])
    assert [t.name for t in changes.tools_added] == ["a"]
    assert [t.name for t in changes.tools_removed] == ["a"]


def test_identical_declarations_produce_no_changes() -> None:
    changes = get_tool_state_changes([tool("a")], [tool("a")])
    assert changes.tools_added == () and changes.tools_removed == ()


def test_tool_state_diff_normalizes_each_declaration_once(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = [tool(f"tool_{index}") for index in range(3)]
    current = [tool(f"tool_{index}") for index in range(3)]
    current[1].parameters = {"type": "object", "required": ["updated"]}
    normalize = messages_module.to_tool_declaration
    normalized_names: list[str] = []

    def counting_normalize(item):
        normalized_names.append(item.name)
        return normalize(item)

    monkeypatch.setattr(messages_module, "to_tool_declaration", counting_normalize)

    changes = messages_module.get_tool_state_changes(previous, current)

    assert len(normalized_names) == len(previous) + len(current)
    assert [item.name for item in changes.tools_added] == ["tool_1"]
    assert [item.name for item in changes.tools_removed] == ["tool_1"]


def test_declaration_comparison_ignores_execution_only_fields() -> None:
    """transcript.ts:123-130 `toToolDeclaration` 只留 name/description/parameters(+constrainedSampling)。

    StoryIDE 侧的含义：label、执行模式、replay 策略、审批标记都是宿主/执行期信息，
    不参与「模型看到没看到变化」的判定，否则每次换实现都会误报工具集变更。
    """
    plain = tool("a")
    with_hidden = tool("a", label="写章节", execution_mode="sequential", replay="never")
    assert to_tool_declaration(with_hidden).parameters == plain.parameters
    assert not hasattr(to_tool_declaration(with_hidden), "label")
    assert declarations_equal(plain, with_hidden)
    assert not declarations_equal(plain, tool("a", schema={"type": "object", "required": ["x"]}))


def test_redefinition_and_non_additive_change_detection() -> None:
    """transcript.ts:183-208：同名不同定义 = 重定义；出现 remove 或重定义 = 非纯增量。"""
    same_name_diff = [
        SystemMessage(tools_added=[tool("a")]),
        SystemMessage(tools_added=[tool("a", {"type": "object", "required": []})]),
    ]
    pure_addition = [SystemMessage(tools_added=[tool("a")]), SystemMessage(tools_added=[tool("b")])]
    assert has_tool_redefinitions(same_name_diff) is True
    assert has_non_additive_tool_changes(same_name_diff) is True
    assert has_tool_redefinitions(pure_addition) is False
    assert has_non_additive_tool_changes(pure_addition) is False


# --- 能力开关下的两条路径（P01 hazard 的解） --------------------------------


def test_endpoints_without_mid_conversation_system_messages_collapse_them() -> None:
    """transcript.ts:108-120：不支持时把后续 system 消息折进最前面那一条，而不是丢掉内容。"""
    transcript = normalize_context(
        AgentContext(
            messages=[
                _user("作者问的第一句"),
                SystemMessage(content="新增：保持时态"),
                _user("第二句"),
            ],
            system_prompt="基线",
            tools=[tool("a")],
        )
    )
    collapsed = resolve_transcript(transcript, supports_mid_convo_system_messages=False)
    roles = [m.role for m in collapsed.messages]
    assert roles[0] == "system"
    assert "新增：保持时态" in collapsed.messages[0].content
    assert roles.count("system") == 1, "collapse 之后只能剩一条 system 消息"
    # 支持时原样保留，交给 Provider Adapter 决定怎么发
    kept = resolve_transcript(transcript, supports_mid_convo_system_messages=True)
    assert [m.role for m in kept.messages].count("system") == 2


def test_tool_request_split_follows_the_additions_capability() -> None:
    """transcript.ts:226-234：能锚定增量才只发初始工具，否则发全量当前工具集。"""
    transcript = normalize_context(AgentContext(messages=[], system_prompt="p", tools=[tool("a")]))
    transcript_with_addition = AgentContext(
        messages=[*transcript.messages, SystemMessage(tools_added=[tool("b")])],
    )
    anchored = resolve_transcript_tools(transcript_with_addition.messages, supports_tool_additions=True)
    assert anchored.anchors_additions is True
    assert [t.name for t in anchored.request_tools] == ["a"]

    not_anchored = resolve_transcript_tools(transcript_with_addition.messages, supports_tool_additions=False)
    assert not_anchored.anchors_additions is False
    assert [t.name for t in not_anchored.request_tools] == ["a", "b"]


def test_removal_forces_full_tool_set_even_when_additions_are_supported() -> None:
    transcript = normalize_context(AgentContext(messages=[], system_prompt="p", tools=[tool("a"), tool("b")]))
    messages = [*transcript.messages, SystemMessage(tools_removed=[tool("a")])]
    resolved = resolve_transcript_tools(messages, supports_tool_additions=True)
    assert resolved.anchors_additions is False
    assert [t.name for t in resolved.request_tools] == ["b"]


def test_render_system_message_update_names_removed_sections() -> None:
    """text.ts:28-40：删掉的 section 要渲染成明示文本，不能静默消失。"""
    rendered = render_system_message_update(SystemMessage(sections={"字数": None, "文风": "白描"}))
    assert 'Removed system prompt section "字数".' in rendered
    assert 'Updated system prompt section "文风"' in rendered


def test_collapse_is_idempotent_and_keeps_non_system_order() -> None:
    transcript = normalize_context(
        AgentContext(
            messages=[_user("u1"), SystemMessage(content="中"), _user("u2")],
            system_prompt="头",
        )
    )
    once = collapse_system_messages(transcript)
    twice = collapse_system_messages(once)
    assert [m.role for m in twice.messages] == ["system", "user", "user"]
    assert once.messages == twice.messages


def _user(text: str):
    from yanxu_harness.core.types import UserMessage

    return UserMessage(content=text)


def test_annotation_names_resolve_at_runtime() -> None:
    """Annotations are strings here (`from __future__ import annotations`), so a missing
    `typing` import hides until someone calls `get_type_hints` — which a host doing
    schema or DI work does. `CustomMessageRegistry` referenced `Callable`/`Mapping`
    without importing them."""
    import typing

    registry = messages_module.CustomMessageRegistry()
    hints = typing.get_type_hints(registry.register)
    assert hints["to_llm"] == typing.Callable[[typing.Any], typing.Any]
    assert hints["from_stored"] == typing.Optional[typing.Callable[[typing.Mapping[str, typing.Any]], typing.Any]]
