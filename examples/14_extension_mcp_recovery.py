#!/usr/bin/env python3
"""Assemble an optional MCP extension pack, pause on a write, restart and resume.

    python examples/14_extension_mcp_recovery.py

Requires the ``mcp`` extra. The MCP server is the repository's local fixture; no
credential, paid model or external service is used. A base install prints a
machine-readable "skipped" record and exits 0, so the base gate can prove the
missing extra is an explicit *open-time* error rather than an import failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "examples" / "extensions", ROOT / "tests" / "mcp"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from mcp_reference_pack import MCP_SKILL_ID, build_mcp_reference_pack  # noqa: E402

from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall  # noqa: E402
from yanxu_harness.extensions import open_extensions  # noqa: E402
from yanxu_harness.integrations.mcp import sdk_available  # noqa: E402
from yanxu_harness.runtime import Principal, RunContext, RunIdentity, RunRequest  # noqa: E402
from yanxu_harness.storage import (  # noqa: E402
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.tools import (  # noqa: E402
    ApprovalStatus,
    ConservativePolicy,
    GatewayBindings,
    ToolGateway,
)


class Script:
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
    if not sdk_available():
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "the optional 'mcp' extra is not installed",
                    "install": "pip install 'yanxu-harness[mcp]'",
                },
                sort_keys=True,
            )
        )
        return 0

    from support.harness import counter, facts_of, mcp_stdio_config

    with tempfile.TemporaryDirectory(prefix="yanxu-mcp-extension-") as directory:
        root = Path(directory)
        facts = root / "facts.jsonl"
        state = root / "state"
        state.mkdir()
        config = mcp_stdio_config(
            facts,
            state,
            protocol="2025-11-25",
            server_id="fixture",
            authorization_scope="authorized",
            config_revision="cfg-1",
        )
        pack = build_mcp_reference_pack(config=config)
        context = RunContext(
            identity=RunIdentity(
                namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"
            ),
            principal=Principal(subject="example"),
            labels={"authorization_scope": "authorized"},
        )
        first_stream = Script(
            AssistantMessage(
                content=[ToolCall(id="call-1", name=pack.bump, arguments={"key": "alpha"})],
                stop_reason=StopReason.TOOL_USE,
            )
        )

        first = await open_extensions(pack.plan).open()
        stores = ExitStack()
        try:
            approvals = stores.enter_context(SqliteApprovalStore(root / "approvals.db"))
            backend = stores.enter_context(SqliteExecutionBackend(root / "operations.db"))
            persistence = stores.enter_context(SqlitePersistence(root / "runs.db"))
            gateway = ToolGateway(
                registry=first.registry,
                policy=ConservativePolicy(),
                approvals=approvals,
                backend=backend,
            )
            bindings = GatewayBindings(gateway=gateway, context=lambda: context)
            harness = first.bind_harness(
                stream_fn=first_stream,
                gateway_bindings=bindings,
                persistence=persistence,
                system_prompt="Use the remote tools only through the gateway.",
            )
            paused = await harness.start(RunRequest(prompt="Bump alpha."), context)
            assert paused.status.value == "suspended", paused.status
            key = paused.pending_interaction["approval_binding_key"]
            fingerprint = first.fingerprint
            await approvals.decide(key, ApprovalStatus.APPROVED, "example-operator")
        finally:
            try:
                report = await first.aclose()
                assert report.ok
            finally:
                stores.close()

        final_stream = Script(
            AssistantMessage(
                content=[TextContent(text="The remote bump is recorded.")],
                stop_reason=StopReason.STOP,
            )
        )
        # A closed factory is not reopened: a new deployment gets a new plan/factory,
        # while the logical deployment identity stays the same.
        pack = build_mcp_reference_pack(config=config)
        second = await open_extensions(pack.plan).open()
        stores = ExitStack()
        try:
            approvals = stores.enter_context(SqliteApprovalStore(root / "approvals.db"))
            backend = stores.enter_context(SqliteExecutionBackend(root / "operations.db"))
            persistence = stores.enter_context(SqlitePersistence(root / "runs.db"))
            await approvals.decide(key, ApprovalStatus.APPROVED, "example-operator")
            gateway = ToolGateway(
                registry=second.registry,
                policy=ConservativePolicy(),
                approvals=approvals,
                backend=backend,
            )
            bindings = GatewayBindings(gateway=gateway, context=lambda: context)
            harness = second.bind_harness(
                stream_fn=final_stream,
                gateway_bindings=bindings,
                persistence=persistence,
                system_prompt="Use the remote tools only through the gateway.",
            )
            metadata = await second.skills.list_metadata(scope="project-a")
            outcome = await harness.resume("r1", {}, context)
            summary = {
                "status": outcome.status.value,
                "final_text": outcome.final_text,
                "deployment": second.fingerprint,
                "same_deployment": second.fingerprint == fingerprint,
                "tools": list(second.registry.names()),
                "skill": metadata[0].skill_id if metadata else "",
                "remote_calls": len(facts_of(facts, "server.call_tool.start")),
                "bump_count": counter(state, "alpha"),
                "model_calls": final_stream.calls,
            }
            assert summary["skill"] == MCP_SKILL_ID
        finally:
            try:
                report = await second.aclose()
                assert report.ok
            finally:
                stores.close()
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if outcome.status.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
