"""`tests/agent_core/` 的共享夹具：scripted provider 流、事件记录器、消息构造器。

为什么提成模块而不是各文件复制：P04 的变异证明要同时打 `test_agent_loop.py` 与
`test_tool_execution.py`，两份「差一点的相同夹具」会让某一份悄悄测不到东西。
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from yanxu_harness.core.types import (
    AssistantMessage,
    StopReason,
    TextContent,
    ToolCall,
    ToolSpec,
    UserMessage,
)


class ScriptedStream:
    """一条脚本化 provider 流：按帧迭代，`result()` 给终态消息。

    两个必须存在的细节：
    * 每次调用先 `await asyncio.sleep(0)`。真流一定会挂起；假流不挂起时，一个不收敛的
      循环会把控制权一直攥在协程链里，连 `asyncio.wait_for` 与 faulthandler 都饿死
      （本仓库真就因此「整套测试卡死」过两次）。
    * 第二次及以后的请求返回一条不带工具调用的收尾消息，否则「单条响应 + 工具调用」
      形状的用例按上游语义就是无限续轮。
    """

    def __init__(self, final: AssistantMessage, frames: list[dict] | None = None) -> None:
        self.final = final
        self.frames = list(frames or [])
        self.calls: list[tuple[Any, Any]] = []
        self._index = 0

    async def __call__(self, model: Any, context: Any, options: Any = None) -> "ScriptedStream":
        await asyncio.sleep(0)
        if self.calls:
            self.final = wrap_up_message()
        self.calls.append((model, context, options))
        return self

    def __aiter__(self) -> "ScriptedStream":
        return self

    async def __anext__(self) -> dict:
        if self._index >= len(self.frames):
            raise StopAsyncIteration
        frame = self.frames[self._index]
        self._index += 1
        return frame

    async def result(self) -> AssistantMessage:
        return self.final


def wrap_up_message() -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text="收尾")], stop_reason=StopReason.STOP)


class SteppingStream(ScriptedStream):
    """按次序每次请求返回不同的 assistant 消息；用尽后重复最后一条。

    必须是子类而不是给实例赋 `__call__`：特殊方法按类型查找，实例赋值不生效 ——
    第一版就是这么写的，于是第二次请求拿到同一条带工具调用的消息，循环永不收敛。
    """

    def __init__(self, *messages: AssistantMessage) -> None:
        super().__init__(messages[0])
        self._queue = list(messages)

    async def __call__(self, model: Any, context: Any, options: Any = None) -> "SteppingStream":
        await asyncio.sleep(0)
        self.calls.append((model, context, options))
        self.final = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        return self


def stepping(*messages: AssistantMessage) -> SteppingStream:
    return SteppingStream(*messages)


def assistant(*blocks: Any, stop: StopReason = StopReason.STOP) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), stop_reason=stop)


def user(text: str) -> UserMessage:
    return UserMessage(content=text)


def tool_call(call_id: str, name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments if arguments is not None else {})


def make_tool(name: str, execute: Any, **kwargs: Any) -> ToolSpec:
    return ToolSpec(name=name, description=name, parameters={"type": "object"}, execute=execute, **kwargs)


async def ok_result(_call_id: str = "", _args: Any = None, _signal: Any = None, _on_update: Any = None) -> dict:
    """签名与上游一致：(tool_call_id, params, signal, on_update)。

    写成 `ok_result(text="ok")` 时 4 个位置参数会抛 TypeError，而 core 把异常转成错误结果 ——
    一条本该「成功」的用例会悄悄变成 `is_error=True`。
    """
    return {"content": [TextContent(text="ok")], "details": {}}


@dataclass
class RecordingSink:
    events: list[Any] = field(default_factory=list)

    async def __call__(self, event: Any) -> None:
        self.events.append(event)

    @property
    def types(self) -> list[str]:
        return [getattr(e, "type", "?") for e in self.events]

    def count(self, name: str) -> int:
        return self.types.count(name)

    def of(self, cls: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, cls)]


def run(coro: Any) -> Any:
    """每个异步场景都带超时：死锁必须是失败，不能是挂住。"""

    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(guard())
