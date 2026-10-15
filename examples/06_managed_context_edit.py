"""Example 6 — a minimal coding agent with managed context (C3 of the context plan).

Run it with no arguments, offline, from anywhere:

    python examples/06_managed_context_edit.py

The chain:

```text
task → the model reads a file → the model asks to write a fix
     → the model answers with what it read and wrote
```

Every request passes through the managed context path: a `ContextController` measures the
request against the model's window, projects the transcript into complete fragments, drops
the oldest removable ones when the window is tight, replays the effective instructions and
tool set into a baseline, and hands the view back to the loop — which commits the view and
its facts *before* the provider is called.

The trial proves four things rather than printing a success line:

* the transcript (`RunRecord.messages`) keeps every message while the view sent to the
  model is smaller — a projection is a view, never the history;
* the facts the controller committed describe the request that was actually sent;
* the tool round trip is never cut in half (no lone `toolResult` in any view);
* the run completes with the model echoing what the tools really returned.

What is real: the model loop, the provider→core adapter, the harness with its SQLite run
store, the tool gateway (policy, intent, receipt) and the context measurement/projection.
What is simulated, and labelled: the model. `ScriptedCodingModel` implements the public
`Provider` protocol and decides from the request it receives; it calls nothing.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

from yanxu_harness.context import (
    ContextController,
    ContextWindow,
    HeuristicTokenEstimator,
    MeasurementInput,
    UnknownPolicy,
)
from yanxu_harness.core.messages import normalize_context, to_llm_messages
from yanxu_harness.core.types import AgentContext, TextContent
from yanxu_harness.providers import (
    CoreStreamBridge,
    ProviderEvent,
    ProviderEventKind,
    ProviderRequest,
    Provider,
    to_provider_request,
)
from yanxu_harness.runtime import DefaultHarness, Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.storage import (
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

#: A window small on purpose: the third request no longer fits the whole transcript, so the
#: projection has to drop complete turns. A host picks these numbers from its real model.
WINDOW = ContextWindow(limit_tokens=340, output_reserve=96, safety_margin=8)
MAX_PAYLOAD_BYTES = 32_000

NAMESPACE = "demo"
SESSION = "coding"
RUN_ID = "managed-1"


# --- the scripted model --------------------------------------------------------


class ScriptedCodingModel(Provider):
    """A deterministic coding model, and a witness of what each request carried.

    It reads the request it is given: with no tool result it asks for the read; after the
    read it asks for the write; after the write it answers with both real results echoed,
    which is why the final text proves they travelled through the loop. It also records the
    messages of every request, so the example can compare the views with the transcript.
    """

    name = "scripted-coding-model"

    def __init__(self) -> None:
        self.views: list[list[Mapping[str, Any]]] = []

    async def stream(
        self, request: ProviderRequest, cancellation: Any = None
    ) -> AsyncIterator[ProviderEvent]:
        self.views.append([dict(message) for message in request.messages])
        results = [message for message in request.messages if message.get("role") == "tool"]
        # The script keys off the *newest* result on purpose: an older round trip may have
        # been projected away, and a model that depended on it would re-do work. That is a
        # real property of compaction, not a quirk of this example.
        newest = str(results[-1].get("content") or "") if results else ""
        if not results:
            yield ProviderEvent(
                kind=ProviderEventKind.TOOL_CALL,
                sequence=0,
                tool_call_id="call-read",
                tool_name="read_file",
                arguments_json=json.dumps({"path": "main.py"}),
            )
            yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="tool_calls")
            return
        if "wrote" not in newest:
            yield ProviderEvent(
                kind=ProviderEventKind.TOOL_CALL,
                sequence=0,
                tool_call_id="call-write",
                tool_name="write_file",
                arguments_json=json.dumps(
                    {"path": "main.py", "text": "def add(a, b):\n    return a + b\n"}
                ),
            )
            yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="tool_calls")
            return
        yield ProviderEvent(
            kind=ProviderEventKind.TEXT_DELTA,
            sequence=0,
            text=(
                "修好了。最近一次工具报告："
                f"{newest.strip()}（更早的读取结果可能已被投影掉，所以这里只引用最新的）"
            ),
        )
        yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="stop")


# --- tools ---------------------------------------------------------------------


def read_definition(workspace: Path) -> ToolDefinition:
    def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        body = (workspace / arguments["path"]).read_text(encoding="utf-8")
        return {"content": [TextContent(text=body)], "details": {"lines": len(body.splitlines())}}

    return ToolDefinition(
        "read_file",
        "read a file from the workspace",
        effects=frozenset({ToolEffect.READ}),
        args_validator=lambda arguments: {"path": str(arguments["path"])},
        handler=handler,
        preview=lambda _context, arguments: {"path": arguments["path"]},
    )


def write_definition(workspace: Path) -> ToolDefinition:
    def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        target = workspace / arguments["path"]
        target.write_text(arguments["text"], encoding="utf-8")
        return {
            "content": [TextContent(text=f"wrote {len(arguments['text'])} bytes to {target.name}")],
            "details": {"bytes": len(arguments["text"])},
        }

    return ToolDefinition(
        "write_file",
        "write a file in the workspace",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=lambda arguments: {
            "path": str(arguments["path"]),
            "text": str(arguments["text"]),
        },
        handler=handler,
        preview=lambda _context, arguments: {
            "path": arguments["path"],
            "bytes": len(arguments["text"]),
        },
    )


# --- the request conversion the controller must measure -------------------------


def wire_measurement(messages: Sequence[Any]) -> MeasurementInput:
    """The same conversion the bridge uses, so the measurement is of the sent request.

    `CoreStreamBridge` turns the loop's transcript into a provider request with
    `to_provider_request`; the controller is given that same function, which is what makes
    "the measured request is the sent request" a fact instead of an intention.
    """
    llm = to_llm_messages(list(messages))
    request = to_provider_request(None, normalize_context(AgentContext(messages=llm)))
    return MeasurementInput.from_request(request)


# --- the run -------------------------------------------------------------------


async def trial(workspace: Path) -> dict[str, Any]:
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    model = ScriptedCodingModel()
    controller = ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        # The same conversion the bridge performs, so the measurement and the wire agree.
        converter=wire_measurement,
        window=WINDOW,
        max_payload_bytes=MAX_PAYLOAD_BYTES,
        on_unknown=UnknownPolicy.PROCEED,
    )
    gateway = ToolGateway(
        registry=ToolRegistry([read_definition(workspace), write_definition(workspace)]),
        policy=ConservativePolicy(auto_write=True),
        approvals=SqliteApprovalStore(state / "approvals.db"),
        backend=SqliteExecutionBackend(state / "operations.db"),
    )
    bindings = GatewayBindings(
        gateway=gateway,
        context=lambda: RunContext(
            identity=RunIdentity(
                namespace=NAMESPACE, session_id=SESSION, run_id=RUN_ID, attempt_id="a1"
            ),
            principal=Principal(subject="author"),
        ),
    )
    persistence = SqlitePersistence(state / "runs.db")
    harness = DefaultHarness(
        stream_fn=CoreStreamBridge(provider=model),
        tools=[
            core_tool(read_definition(workspace), bindings),
            core_tool(write_definition(workspace), bindings),
        ],
        persistence=persistence,
        system_prompt="You are a careful coding agent. Read before you write.",
        context_controller=controller,
    )
    context = RunContext(
        identity=RunIdentity(namespace=NAMESPACE, session_id=SESSION, run_id=RUN_ID, attempt_id="a1"),
        principal=Principal(subject="author"),
    )
    outcome = await harness.start(
        RunRequest(prompt="修复 main.py 里的 add", max_turns=6), context
    )
    record = (await persistence.load_run(RUN_ID)).state
    persistence.close()
    return {"outcome": outcome, "record": record, "model": model}


def check(result: Mapping[str, Any]) -> list[str]:
    """Assert the trial's claims; return the lines worth printing."""
    outcome = result["outcome"]
    record = result["record"]
    model: ScriptedCodingModel = result["model"]
    facts = record.get("context_state") or {}
    transcript_roles = [message["role"] for message in record["messages"]]
    views = [[dict(message) for message in view] for view in model.views]
    view_roles = [[message.get("role") for message in view] for view in views]
    lines: list[str] = []

    assert outcome.status.value == "completed", f"the trial must complete, got {outcome.status}"
    assert len(model.views) == 3, f"three turns expected, got {len(model.views)}"
    assert transcript_roles.count("toolResult") == 2, transcript_roles
    lines.append(f"transcript kept      : {len(record['messages'])} messages {transcript_roles}")
    lines.append(
        f"wire views           : {[len(view) for view in views]} provider messages {view_roles}"
    )

    # A projection is a view, never the history: the transcript keeps every message, while
    # at least one request went out with fewer messages than the transcript had.
    request_view = (record.get("execution") or {}).get("request_view") or []
    assert facts.get("removed_messages", 0) >= 1, "a dropped fragment must be reported"
    assert facts["baseline_synthesized"] is True, (
        "dropping the instruction fragment must leave a replayed baseline"
    )
    assert len(request_view) < len(record["messages"]), (
        "the committed view must be smaller than the transcript it was derived from"
    )
    assert facts["dropped"] and all(entry["reason"] for entry in facts["dropped"])
    lines.append(
        "projection           : "
        f"kept {facts['kept_fragments']}, removed {facts['removed_messages']} messages, "
        f"baseline_synthesized={facts['baseline_synthesized']}"
    )
    lines.append(
        "measured             : "
        f"{facts['estimated_input_tokens']} tokens of {facts['capacity']} capacity "
        f"({facts['estimator_id']}/{facts['estimator_method']}, exact={facts['estimate_exact']})"
    )

    # No view may cut a tool round trip in half.
    for index, view in enumerate(views):
        declared = {
            call["id"]
            for message in view
            for call in (message.get("tool_calls") or ())
        }
        answered = {message.get("tool_call_id") for message in view if message.get("role") == "tool"}
        assert not (answered - declared), f"view {index} carries a result whose call was dropped"
    # The facts describe the request that was sent last.
    assert facts["request_fingerprint"], "the committed facts name the request"
    assert request_view, "the prepared view is committed with the facts"
    assert facts["scope"] == f"{NAMESPACE}/{SESSION}/{RUN_ID}"
    lines.append(f"committed facts      : scope={facts['scope']} fingerprint={facts['request_fingerprint'][:21]}…")

    # The tool results the model saw are the ones the tools produced.
    final = record["messages"][-1]
    answer = "".join(block.get("text", "") for block in final["content"])
    assert "工具报告" in answer and "bytes" in answer, answer
    lines.append(f"model answer         : {answer}")

    # And the file really changed.
    assert "return a + b" in (result["workspace_written"] or ""), result["workspace_written"]
    lines.append("workspace            : main.py now contains the fixed add()")
    return lines


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yanxu-managed-context-") as directory:
        workspace = Path(directory)
        (workspace / "main.py").write_text(
            "# a small module\n"
            "def add(a, b):\n"
            "    return a - b  # bug: should add\n"
            "\n"
            "def unused():\n"
            "    return 'kept for the transcript to be large enough to project'\n" * 1,
            encoding="utf-8",
        )
        result = await trial(workspace)
        result["workspace_written"] = (workspace / "main.py").read_text(encoding="utf-8")
        try:
            lines = check(result)
        except AssertionError as failure:
            print(f"FAILED: {failure}")
            return 1
    print("Managed context trial (offline, scripted model)")
    for line in lines:
        print(f"  {line}")
    print("OK: the run completed, the transcript stayed whole, and the view was projected.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
