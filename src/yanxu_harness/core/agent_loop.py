"""Compatibility module: the loop's public names, now implemented by one Runner.

Until S3 this file held the two-layer loop. The behaviour now lives in:

* :mod:`yanxu_harness.core.loop_config` — :class:`AgentLoopConfig` and the hook payloads,
* :mod:`yanxu_harness.core.transitions` — the pure reducer that decides,
* :mod:`yanxu_harness.core.runner` — :class:`Runner`, the single driver, and the entry
  points ``run_agent_loop`` / ``run_agent_loop_continue`` (+ their ``*_result`` forms).

Everything is re-exported here unchanged, so ``from yanxu_harness.core.agent_loop import
AgentLoopConfig, run_agent_loop`` keeps working and keeps referring to the same objects
— one loop, two import paths, exactly like ``yanxu_harness.runtime.batch`` re-exports
``CallOutcome``. The loop/message vocabulary was written against
``@earendil-works/pi-agent-core`` 0.86.1 (MIT, commit ``c7cdb46``); see
``THIRD_PARTY_NOTICES.md``. No equivalence with the whole upstream package is claimed.
"""

from __future__ import annotations

from .loop_config import (
    AgentLoopConfig,
    PrepareNextTurnContext,
    TurnEndContext,
    validate_max_turns,
)
from .runner import (
    EventSink,
    run_agent_loop,
    run_agent_loop_continue,
    run_agent_loop_continue_result,
    run_agent_loop_result,
)

__all__ = [
    "AgentLoopConfig",
    "EventSink",
    "PrepareNextTurnContext",
    "TurnEndContext",
    "run_agent_loop",
    "run_agent_loop_continue",
    "run_agent_loop_continue_result",
    "run_agent_loop_result",
    "validate_max_turns",
]
