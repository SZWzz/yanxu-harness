"""The neutral core: messages, one model loop, tool batches, streaming, queues, cancellation.

This is the **only** model loop in the dependency closure. The host package
(`backend/agent_core`) re-exports these same objects through thin shims, so a
type check, an `except` clause or a pickle cannot split between two
implementations; there is no second loop and no fallback engine.

Dependency direction: this package imports the standard library only. It never
imports the application layer (web framework, application store, story modules,
provider clients, host configuration). The guard is
`packages/agent-harness/tests/test_import_fence.py` plus the host-side
`tests/test_agent_core_boundaries.py`, which scans this directory as the
authority rather than the shim.

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

from .messages import normalize_context, resolve_transcript
from .stream import EventStream, create_assistant_message_event_stream
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
