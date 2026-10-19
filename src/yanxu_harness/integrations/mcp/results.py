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

import base64
import json
from dataclasses import replace
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from ...tools.execution import EffectState, ToolExecutionResult
from .config import McpServerConfig
from .types import McpCallFacts, McpCallResult

__all__ = ["MAX_MODEL_TEXT_CHARS", "SecretRedactor", "codec_result"]

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


class SecretRedactor:
    """Removes known host credentials from every remote-derived surface.

    The adapter is the last place where the host still knows which strings are its own
    credentials: after this boundary the text goes into a neutral result, the ledger,
    events and the next model request. Redaction therefore happens *here*, before any of
    those, and it covers text, nested structured values, error details and exception
    messages. Nothing downstream has to re-scan for secrets, and no secret is copied into
    generic facts for a post-processor to find.

    The values come from the host's own ``McpServerConfig``: every HTTP header value and
    every environment value whose key looks credential-shaped. Header values are also
    split into their authorization scheme and credential, and common encoded forms are
    included so an upstream that echoes a base64 or URL-encoded copy is still cleaned.
    """

    PLACEHOLDER = "[REDACTED]"
    #: Values shorter than this are not treated as secrets. An environment variable whose
    #: *name* merely looks credential-shaped may legitimately hold a short word (a scope
    #: name, a mode), and replacing every occurrence of it would corrupt useful text while
    #: protecting nothing a reader could not guess.
    MIN_SECRET_LENGTH = 8
    _SENSITIVE_KEY = (
        "token",
        "secret",
        "password",
        "passwd",
        "credential",
        "authorization",
        "auth",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
    )
    _AUTH_SCHEMES = frozenset({"bearer", "basic", "token", "digest", "apikey", "api-key"})
    MAX_DEPTH = 32

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values: set[str] = set()
        for secret in secrets:
            if not isinstance(secret, str):
                continue
            self._collect(secret, values)
        self._secrets = tuple(sorted(values, key=len, reverse=True))

    @classmethod
    def from_config(cls, config: McpServerConfig) -> "SecretRedactor":
        secrets: list[str] = []
        for value in dict(config.headers).values():
            if isinstance(value, str):
                secrets.append(value)
                parts = value.split(None, 1)
                if len(parts) == 2 and parts[0].lower() in cls._AUTH_SCHEMES:
                    secrets.append(parts[1])
        for name, value in dict(config.env).items():
            lowered = str(name).lower()
            if isinstance(value, str) and any(token in lowered for token in cls._SENSITIVE_KEY):
                secrets.append(value)
        # A credential embedded in argv is the same leak as one in a header.
        for argument in config.args:
            if not isinstance(argument, str) or "=" not in argument:
                continue
            flag, _, value = argument.partition("=")
            if any(token in flag.lower().lstrip("-") for token in cls._SENSITIVE_KEY):
                secrets.append(value)
        return cls(secrets)

    @classmethod
    def _collect(cls, secret: str, found: set[str]) -> None:
        value = secret.strip()
        if len(value) < cls.MIN_SECRET_LENGTH:
            return
        found.add(value)
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        if encoded != value:
            found.add(encoded)
        quoted = quote(value, safe="")
        if quoted != value:
            found.add(quoted)
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        if escaped != value:
            found.add(escaped)

    @property
    def active(self) -> bool:
        return bool(self._secrets)

    def text(self, value: str) -> str:
        if not self._secrets or not isinstance(value, str):
            return value
        result = value
        for secret in self._secrets:
            if secret in result:
                result = result.replace(secret, self.PLACEHOLDER)
        return result

    def value(self, value: Any, *, _depth: int = 0) -> Any:
        """Redact every string inside a JSON-like value, preserving its shape."""
        if not self._secrets:
            return value
        if _depth > self.MAX_DEPTH:
            return self.PLACEHOLDER
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {
                self.text(key) if isinstance(key, str) else key: self.value(item, _depth=_depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.value(item, _depth=_depth + 1) for item in value]
        if isinstance(value, (set, frozenset)):
            return [self.value(item, _depth=_depth + 1) for item in value]
        return value

    def call_result(self, answer: McpCallResult) -> McpCallResult:
        """Sanitize one remote answer before the codec turns it into a neutral result."""
        if not self.active:
            return answer
        return replace(
            answer,
            text=tuple(self.text(item) for item in answer.text),
            structured=self.value(answer.structured),
        )

    def execution_result(self, result: ToolExecutionResult) -> ToolExecutionResult:
        """Sanitize the final neutral result, including facts merged from the entry."""
        if not self.active:
            return result
        return ToolExecutionResult(
            state=result.state,
            result=self.value(result.result),
            detail=self.text(result.detail),
            details=self.value(dict(result.details)),
            external_id=None if result.external_id is None else self.text(result.external_id),
        )


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
