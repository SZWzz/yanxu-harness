#!/usr/bin/env python3
"""Assemble a standard-library extension pack and run one real tool call.

    python examples/13_extension_tools_skills.py

The model is scripted; the harness, gateway, approvals, persistence and tool
execution are the real implementations. The pack is the repository's reference
extension (``examples/extensions/reference_pack.py``), not a special test double.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "examples" / "extensions") not in sys.path:
    sys.path.insert(0, str(ROOT / "examples" / "extensions"))

from reference_pack import LOOKUP_TOOL, REFERENCE_SKILL_ID, build_reference_plan  # noqa: E402

from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall  # noqa: E402
from yanxu_harness.extensions import open_extensions  # noqa: E402
from yanxu_harness.runtime import (  # noqa: E402
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)
from yanxu_harness.storage import InMemoryPersistence  # noqa: E402
from yanxu_harness.testing import RecordingExecutionBackend  # noqa: E402
from yanxu_harness.tools import (  # noqa: E402
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolGateway,
)


class Script:
    """One assistant message per model request."""

    def __init__(self, *messages: AssistantMessage) -> None:
        self.messages = list(messages)
        self.calls = 0

    async def __call__(self, model: Any, context: Any, options: Any = None):
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self) -> Mapping[str, Any]:
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return self._final


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yanxu-extension-example-") as directory:
        workspace = Path(directory) / "workspace"
        plan = build_reference_plan(workspace=workspace)
        stream = Script(
            AssistantMessage(
                content=[ToolCall(id="call-1", name=LOOKUP_TOOL, arguments={"term": "alpha"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            AssistantMessage(
                content=[TextContent(text="Alpha is the first entry.")],
                stop_reason=StopReason.STOP,
            ),
        )
        context = RunContext(
            identity=RunIdentity(
                namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"
            ),
            principal=Principal(subject="example"),
            labels={"authorization_scope": "authorized"},
        )

        async with open_extensions(plan) as deployment:
            gateway = ToolGateway(
                registry=deployment.registry,
                policy=ConservativePolicy(),
                approvals=InMemoryApprovalStore(),
                backend=RecordingExecutionBackend(),
            )
            bindings = GatewayBindings(gateway=gateway, context=lambda: context)
            harness = deployment.bind_harness(
                stream_fn=stream,
                gateway_bindings=bindings,
                persistence=InMemoryPersistence(),
                system_prompt="Use the reference pack when a term is requested.",
            )
            metadata = await deployment.skills.list_metadata(scope="project-a")
            body = await deployment.skills.load_body(REFERENCE_SKILL_ID, scope="project-a")
            guide = await deployment.skills.read_resource(
                REFERENCE_SKILL_ID, "guide.txt", scope="project-a"
            )
            outcome = await harness.start(RunRequest(prompt="Look up alpha."), context)
            summary = {
                "status": outcome.status.value,
                "final_text": outcome.final_text,
                "deployment": deployment.fingerprint,
                "tools": list(deployment.registry.names()),
                "skill": metadata[0].skill_id if metadata else "",
                "skill_chars": len(body.text),
                "resource": guide.text,
                "model_calls": stream.calls,
                "journal": (workspace / "reference-writes.jsonl").exists(),
            }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if outcome.status.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
