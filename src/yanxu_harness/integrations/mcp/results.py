"""结果 codec：把一次远端回答变成中立 `ToolExecutionResult`。

规则（计划 §6.2）逐条落地：

* 完整合法 terminal 成功 → SUCCEEDED，内容仍是远端返回的来源；
* 完整合法 terminal ``isError=true`` → **FAILED**（不是成功回执），有界错误正文进 detail
  让模型看得到，完整内容留在 receipt；不代表业务已回滚，也不自动重试；
* 纯 structured content → 生成确定性的 TextContent 表示，避免既有 bridge 把没有 content
  的 mapping 变成空结果；
* 非 terminal（请求更多交互）、媒体不支持、无法解释的形态 → 明确 unsupported：
  mutating 工具 UNKNOWN，只读工具 FAILED；绝不当成空成功，也绝不在 codec 里再发请求；
* 一切文本/结构化内容都有界；模型视图可以更短，但不能截坏 JSON 冒充完整数据。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ...tools.execution import EffectState, ToolExecutionResult
from .types import McpCallFacts, McpCallResult

__all__ = ["MAX_MODEL_TEXT_CHARS", "codec_result"]

#: How much text the model view may carry in total. The receipt keeps the bounded
#: remote content; the context layer shortens further through the tool's
#: `result_char_limit` when it needs to.
MAX_MODEL_TEXT_CHARS = 8000

#: One structured payload rendered into the model view is bounded too.
MAX_STRUCTURED_TEXT_CHARS = 4000


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _block(text: str, *, kind: str = "text") -> Mapping[str, Any]:
    return {"type": kind, "text": text}


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[truncated: {len(text) - limit} chars]"


def codec_result(
    answer: McpCallResult,
    *,
    facts: McpCallFacts,
    mutating: bool,
) -> ToolExecutionResult:
    """Turn one complete remote answer into a neutral result."""
    details: dict[str, Any] = dict(facts.as_details())

    if answer.requests_more:
        details["unsupported_kind"] = "interaction_required"
        return ToolExecutionResult(
            state=EffectState.UNKNOWN if mutating else EffectState.FAILED,
            detail=(
                f"{answer.name}: the server asked for another interaction round "
                "(sampling/elicitation/input required), which this release does not drive"
            ),
            details=details,
        )

    if answer.unsupported_shape:
        details["unsupported_kind"] = "media_or_shape"
        return ToolExecutionResult(
            state=EffectState.UNKNOWN if mutating else EffectState.FAILED,
            detail=(
                f"{answer.name}: the server returned content this release cannot represent "
                "(non-text media or an unrecognised result shape)"
            ),
            details=details,
        )

    blocks: list[Mapping[str, Any]] = [_block(text) for text in answer.text]
    if answer.structured is not None:
        rendered = _bounded(_canonical(answer.structured), MAX_STRUCTURED_TEXT_CHARS)
        # Deterministic and after the text blocks: a structured-only answer still
        # reaches the model as readable text instead of an empty result.
        blocks.append(_block(rendered))

    if answer.is_error:
        # The server answered: the effect is not proven to have succeeded, but this is
        # NOT a claim that nothing happened, and it is not retried.
        details["remote_is_error"] = True
        remote_text = _bounded("\n".join(answer.text) or "the remote tool reported a failure", 2000)
        detail = f"{answer.name}: the remote tool reported a failure — {remote_text}"
        details["error_text"] = remote_text
        return ToolExecutionResult(
            state=EffectState.FAILED,
            detail=detail,
            details=details,
            # Full (frame-bounded) content stays in the receipt for审查, while the
            # model gets the detail through the refusal projection.
            result={"content": blocks, "structured_content": answer.structured, "is_error": True},
        )

    total = 0
    bounded_blocks: list[Mapping[str, Any]] = []
    for block in blocks:
        text = str(block["text"])
        remaining = MAX_MODEL_TEXT_CHARS - total
        if remaining <= 0:
            bounded_blocks.append(_block("[further content omitted from the model view]"))
            break
        text = _bounded(text, remaining)
        total += len(text)
        bounded_blocks.append(_block(text))

    return ToolExecutionResult(
        state=EffectState.SUCCEEDED,
        result={
            "content": bounded_blocks,
            "structured_content": answer.structured,
        },
        details=details,
    )
