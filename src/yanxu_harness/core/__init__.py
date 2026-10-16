"""The neutral core: messages, one model loop, tool batches, streaming, queues, cancellation.

This is the **only** model loop in the dependency closure. A host that used to vendor
this loop under one of its own module paths is expected to re-export these same objects
through a thin shim (`host_shim.messages is yanxu_harness.core.messages`), so a type
check, an `except` clause or a pickle cannot split between two implementations; the shim
lives in the host, and there is no second loop and no fallback engine here.

Dependency direction: this package imports the standard library only. It never imports
the application layer (web framework, application store, story modules, provider
clients, host configuration). The guard is `tests/test_import_fence.py`, which scans
this directory as the authority and cold-imports every submodule.

Provenance: the loop/message/stream vocabulary was written against
`@earendil-works/pi-agent-core` 0.86.1 (MIT, Copyright (c) 2025 Mario Zechner,
commit `c7cdb460aa8a0cebef3446c4166729b8a0d97ead`), re-implemented in Python for
this project. See `THIRD_PARTY_NOTICES.md`. No equivalence with the whole
upstream package is claimed, and upstream is not a build, test or runtime
dependency.

Contents: `types` / `messages` / `events` / `stream` / `cancellation` / `queues`
(neutral model), `agent_loop` / `tool_execution` (the loop and its tool batches)
and `agent` (the run-level facade). Host-side envelopes, checkpoints and
projections live in the host's own adapters, not here.
"""

from .agent_loop import (
    AgentLoopConfig,
    run_agent_loop,
    run_agent_loop_continue,
    run_agent_loop_continue_result,
    run_agent_loop_result,
)
from .execution import (
    Action,
    AwaitToolObservation,
    BatchResumed,
    CallProgress,
    ExecutionState,
    FinishToolBatch,
    InputEvent,
    Phase,
    ToolBatchDrained,
    ToolBatchSettled,
    ToolCallObserved,
    Transition,
    declared_batch,
    pending_work_of,
    state_from_record,
    state_to_record,
)
from .messages import content_text, normalize_context, resolve_transcript
from .outcome import (
    MAX_TURNS_STOP_CODE,
    RUN_STATUS_TRANSITIONS,
    TERMINAL_STATUSES,
    CallOutcome,
    LoopResult,
    LoopStop,
    LoopSuspended,
    RunStatus,
    assert_transition,
    can_transition,
    classify_status,
    is_terminal_status,
)
from .stream import EventStream, create_assistant_message_event_stream
from .tool_execution import ToolBatch, ToolBatchSession, ToolCallObservation
from .transitions import advance, result_of
from .types import (
    AgentContext,
    AssistantMessage,
    Model,
    StopReason,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    TranscriptContext,
    Usage,
    UserMessage,
)

__all__ = [
    "Action",
    "AgentLoopConfig",
    "AwaitToolObservation",
    "BatchResumed",
    "CallOutcome",
    "CallProgress",
    "ExecutionState",
    "FinishToolBatch",
    "InputEvent",
    "MAX_TURNS_STOP_CODE",
    "Phase",
    "RUN_STATUS_TRANSITIONS",
    "TERMINAL_STATUSES",
    "ToolBatch",
    "ToolBatchDrained",
    "ToolBatchSession",
    "ToolBatchSettled",
    "ToolCallObservation",
    "ToolCallObserved",
    "Transition",
    "declared_batch",
    "LoopResult",
    "LoopStop",
    "LoopSuspended",
    "RunStatus",
    "advance",
    "assert_transition",
    "can_transition",
    "classify_status",
    "content_text",
    "is_terminal_status",
    "pending_work_of",
    "result_of",
    "run_agent_loop",
    "run_agent_loop_continue",
    "run_agent_loop_continue_result",
    "run_agent_loop_result",
    "state_from_record",
    "state_to_record",
    "AgentContext",
    "AssistantMessage",
    "EventStream",
    "Model",
    "StopReason",
    "SystemMessage",
    "TextContent",
    "ThinkingContent",
    "ToolCall",
    "ToolResultMessage",
    "ToolSpec",
    "TranscriptContext",
    "Usage",
    "UserMessage",
    "normalize_context",
    "resolve_transcript",
    "create_assistant_message_event_stream",
]
