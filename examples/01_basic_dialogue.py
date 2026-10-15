"""Example 1 — a basic conversation with an injected provider.

Run it from a directory where `yanxu_harness` is installed, with no network and no
credentials:

    python examples/01_basic_dialogue.py

The provider here is a **clearly marked scripted double**: it replays two frames
and returns a fixed answer. That is the point of the example — the loop, the
transcript, the turn budget and the event stream are the real ones, and a host
that has a real provider only has to replace `ScriptedStream` with, say,
`yanxu_harness.providers.OpenAICompatibleProvider`.
"""

from __future__ import annotations

import asyncio
import sys

from yanxu_harness.core import (
    AgentContext,
    AssistantMessage,
    StopReason,
    TextContent,
    UserMessage,
)
from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop

FRAMES = [
    {"type": "text_delta", "text": "海边的"},
    {"type": "text_delta", "text": "信号塔亮着。"},
]
ANSWER = "海边的信号塔亮着。"


class ScriptedStream:
    """An injected provider: replays `FRAMES`, then reports `ANSWER`.

    Deliberately not a mock of the loop — this is the `stream_fn` seam the loop
    calls, exactly where a real provider adapter goes.
    """

    def __init__(self, frames, answer: str) -> None:
        self._frames = list(frames)
        self._answer = answer
        self._index = 0

    async def __call__(self, model, context, options=None):
        await asyncio.sleep(0)  # a real stream always suspends at least once
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self._index >= len(self._frames):
            raise StopAsyncIteration
        frame = self._frames[self._index]
        self._index += 1
        return frame

    async def result(self) -> AssistantMessage:
        return AssistantMessage(content=[TextContent(text=self._answer)], stop_reason=StopReason.STOP)


async def main() -> int:
    events: list[str] = []

    async def emit(event) -> None:
        events.append(type(event).__name__)

    context = AgentContext(messages=[], system_prompt="Be brief.", tools=[])
    config = AgentLoopConfig(stream_fn=ScriptedStream(FRAMES, ANSWER), max_turns=1)

    transcript = await run_agent_loop([UserMessage(content="海边有什么？")], context, config, None, emit)

    final = [message for message in transcript if isinstance(message, AssistantMessage)][-1]
    text = "".join(block.text for block in final.content if isinstance(block, TextContent))

    print(f"events: {' '.join(events)}")
    print(f"answer: {text}")

    assert text == ANSWER, f"the loop did not produce the scripted answer, got {text!r}"
    assert "TurnStart" in events and "AgentEnd" in events, "the run must have a start and an end"
    print("OK: one model turn, one answer, real loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
