"""工具调用的**框架中立**结果类型（0.6.0 P07）。

为什么要有这一层：`agent_tools.py` 今天跑在 PydanticAI 上，工具失败/重试/挂起分别是
`ToolFailed`/`ModelRetry`/`CallDeferred`。Python core 走的是另一套契约 —— 工具结果里带
`is_error`，挂起由宿主落 durable interaction 并把 run 标成 `suspended`。如果两套引擎各自
读异常类型，网关的授权/幂等/unknown 判定就要写两遍，而「两遍」正是 0.4.0 之后最容易腐坏的形状。

所以规则只有一条：**网关之上只见本模块的类型，各引擎在各自最外层做一次翻译。**
本模块必须是纯标准库 —— 它是 core 路径唯一允许 import 的那半。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ToolArgsInvalid",
    "ToolCancelled",
    "ToolDeferred",
    "ToolRejected",
    "describe_outcome",
    "outcome_of",
]


class ToolOutcome(Exception):
    """网关产出的中立结果基类。"""

    kind = "rejected"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = str(message or "")
        self.details: Mapping[str, Any] = dict(details or {})


class ToolRejected(ToolOutcome):
    """这次调用被拒/失败（原 `ToolFailed`）。模型看得见，宿主按事实记账。"""

    kind = "rejected"


class ToolArgsInvalid(ToolOutcome):
    """参数不合法（原 `ModelRetry`）。语义是「改好了再来」，不是「这次成了」。"""

    kind = "args_invalid"


class ToolDeferred(ToolOutcome):
    """要等作者回答才能继续（原 `CallDeferred`）。宿主落 durable interaction 并把 run 标 suspended。"""

    kind = "deferred"

    def __init__(self, message: str = "", *, metadata: Mapping[str, Any] | None = None,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.metadata: Mapping[str, Any] = dict(metadata or {})


class ToolCancelled(ToolOutcome):
    """run 已被取消（原 pydantic 的 `RunCancelled`）。取消不是失败，两者在宿主侧是不同事实。"""

    kind = "cancelled"


_KINDS = {"rejected": ToolRejected, "args_invalid": ToolArgsInvalid, "deferred": ToolDeferred, "cancelled": ToolCancelled}


@dataclass(frozen=True)
class OutcomeDescription:
    """给两个引擎共用的投影：模型看到什么、宿主记什么。"""

    kind: str
    message: str
    is_error: bool = True
    suspends_run: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


def outcome_of(exc: BaseException) -> ToolOutcome | None:
    """已经是中性的就返回它，其余返回 `None` 交给调用方按「意外异常」处理。

    框架异常**不在这里**探测：网关（`agent_tools`）负责把 handler 抛出的 pydantic 异常折成
    本模块的类型。这一层要是反过来 import pydantic-ai，core 路径就又绑上旧引擎了。
    """

    return exc if isinstance(exc, ToolOutcome) else None


def describe_outcome(outcome: ToolOutcome) -> OutcomeDescription:
    return OutcomeDescription(
        kind=outcome.kind,
        message=outcome.message,
        is_error=outcome.kind != "deferred",
        suspends_run=outcome.kind == "deferred",
        metadata=dict(getattr(outcome, "metadata", {}) or {}),
    )
