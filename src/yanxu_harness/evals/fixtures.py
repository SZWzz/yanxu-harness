"""The frozen fixtures and the offline deployment a case runs against.

Everything here ships with the package, so an evaluation runs from an installed wheel with
no checkout of the repository: the fixture projects are carried inline, the offline model is
a small deterministic adapter, and the tool set is the minimum the cases need (a reader, a
writer, and three sequential probes for the partial-batch cases). It is deliberately *not* a
coding toolbox — the cases measure the harness's protocol, not a product.

The side effect a case cares about is recorded in an append-only trace file **outside** the
workspace: "the handler ran exactly once" is then a fact about the whole evaluation, not
about one process's memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..core.types import AssistantMessage, StopReason, TextContent, ToolCall
from ..errors import ContractError
from ..runtime import DefaultHarness, RunContext, RunIdentity, Principal
from ..storage import SqliteApprovalStore, SqliteExecutionBackend, SqlitePersistence
from ..tools import (
    ConservativePolicy,
    ExecutionMode,
    GatewayBindings,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)
from .types import EvalCase, FixtureSpec

__all__ = [
    "build_deployment",
    "fixture_by_name",
    "FIXTURES",
    "OFFLINE_MODEL",
    "PYTHON",
    "PROBE_TOOLS",
    "ScriptedStream",
    "write_fixture",
]

#: The model id the scripted, deterministic adapter answers as. A case that names anything
#: else is a real-model case and is refused unless it was explicitly configured as one.
OFFLINE_MODEL = "offline"

#: How a fixture's command names the interpreter: a *placeholder*, resolved to the running
#: interpreter when the command runs. Storing an absolute path would put the host's virtualenv
#: into every committed result file, and a versioned case that leaked a machine path would not
#: be reproducible anyway.
PYTHON = "python"

PROBE_TOOLS = ("probe.a", "probe.b", "probe.c")

_CALC = '''"""A tiny module with one bug the task has to find."""


def total(values):
    """Return the sum of ``values``."""
    result = 0
    for value in values[:-1]:
        result += value
    return result
'''

_CALC_TEST = '''from calc import total


def test_total_sums_every_value():
    assert total([1, 2, 3]) == 6


def test_total_of_empty_is_zero():
    assert total([]) == 0
'''

_TASK = """# Task

`calc.total` returns the wrong answer for some inputs. Fix the implementation so that the
project's own tests pass. Change `calc.py`; the tests are the specification and must not be
edited.
"""

FIXTURES: Mapping[str, FixtureSpec] = {
    "calc": FixtureSpec(
        name="calc",
        version="1",
        files={"calc.py": _CALC, "test_calc.py": _CALC_TEST, "task.md": _TASK},
        # The fixture is *supposed* to be broken; a case whose baseline already passes is not
        # measuring anything, so the runner checks this first.
        baseline_command=(PYTHON, "-m", "pytest", "test_calc.py", "-q"),
    ),
    "notes": FixtureSpec(
        name="notes",
        version="1",
        files={"notes.md": "", "task.md": "# Task\n\nWrite the requested line into notes.md.\n"},
    ),
}


def fixture_by_name(name: str) -> FixtureSpec:
    try:
        return FIXTURES[name]
    except KeyError as exc:
        raise ContractError(f"unknown fixture {name!r}") from exc


def write_fixture(spec: FixtureSpec, directory: Path) -> None:
    """Materialise a fixture into ``directory`` (which must be empty or absent)."""
    directory.mkdir(parents=True, exist_ok=True)
    for path, content in spec.files.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


# --- the offline model ------------------------------------------------------------------


class ScriptedStream:
    """A deterministic ``stream_fn``: one message per request, repeating the last one.

    The last message repeats on purpose — a run that fails to stop becomes a visible runaway
    (bounded by the case's turn budget) instead of a hang. ``calls`` counts the *real*
    requests, which is what the protocol facts are stated in.
    """

    def __init__(self, *messages: AssistantMessage) -> None:
        if not messages:
            raise ContractError("a scripted model needs at least one message")
        self.messages = list(messages)
        self.calls = 0
        self._final: AssistantMessage | None = None

    async def __call__(self, model: Any, context: Any, options: Any = None) -> "ScriptedStream":
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self) -> "ScriptedStream":
        return self

    async def __anext__(self) -> Mapping[str, Any]:
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        if self._final is None:  # pragma: no cover - result() is only reached after __call__
            raise ContractError("the scripted model was asked for a result it never produced")
        return self._final


def call_message(call_id: str, name: str, arguments: Mapping[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        stop_reason=StopReason.TOOL_USE,
    )


def probe_message(names: Sequence[str] = PROBE_TOOLS) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(id=f"c-{name}", name=name, arguments={"path": "task.md"}) for name in names
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def final_message(text: str = "done") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


# --- deployment -------------------------------------------------------------------------


@dataclass
class Deployment:
    """One case run's deployment: the harness, its stores, and the paths around them."""

    harness: DefaultHarness
    gateway: ToolGateway
    approvals: SqliteApprovalStore
    workspace: Path
    state: Path
    trace: Path

    def close(self) -> None:
        self.approvals.close()


def _write_definition(workspace: Path, trace: Path, *, version: str = "1") -> ToolDefinition:
    """A file writer whose every execution appends one line to the trace.

    The trace lives outside the workspace so a run cannot rewrite its own evidence, and it is
    appended per execution (not per call) so "exactly once" is a claim about the effect.
    """

    def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        target = workspace / str(arguments["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(arguments["text"]), encoding="utf-8")
        with trace.open("a", encoding="utf-8") as handle:
            handle.write(f"{context.identity.attempt_id}:write_file:{arguments['path']}\n")
        return {"content": [TextContent(text=f"wrote {arguments['path']}")], "details": {}}

    return ToolDefinition(
        "write_file",
        "write a file inside the workspace",
        version=version,
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=lambda arguments: {
            "path": str(arguments["path"]),
            "text": str(arguments["text"]),
        },
        handler=handler,
        preview=lambda context, arguments: {
            "path": str(arguments["path"]),
            "bytes": len(str(arguments["text"]).encode("utf-8")),
        },
    )


def _read_definition(workspace: Path) -> ToolDefinition:
    def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        target = workspace / str(arguments["path"])
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        return {
            "content": [TextContent(text=text)],
            "details": {"bytes": len(text.encode("utf-8"))},
        }

    return ToolDefinition(
        "read_file",
        "read a file inside the workspace",
        args_validator=lambda arguments: {"path": str(arguments["path"])},
        handler=handler,
    )


def _probe_definitions(
    trace: Path, handlers: Mapping[str, Callable[..., Any]] | None = None
) -> list[ToolDefinition]:
    """Three sequential probes: a crash inside one leaves an unambiguous settled prefix."""

    def make(name: str) -> ToolDefinition:
        def counted(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            with trace.open("a", encoding="utf-8") as handle:
                handle.write(f"{context.identity.attempt_id}:{name}:{arguments['path']}\n")
            return {"content": [TextContent(text=f"{name} did {arguments['path']}")], "details": {}}

        return ToolDefinition(
            name,
            f"{name} probe",
            args_validator=lambda arguments: {"path": str(arguments["path"])},
            handler=(handlers or {}).get(name, counted),
            execution_mode=ExecutionMode.SEQUENTIAL,
        )

    return [make(name) for name in PROBE_TOOLS]


def build_deployment(
    *,
    workspace: Path,
    state: Path,
    stream: Any,
    profile: str = "edit",
    attempt: str = "a1",
    auto_write: bool = False,
    handlers: Mapping[str, Callable[..., Any]] | None = None,
    reconcile: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    max_runtime_seconds: float | None = None,
    max_turns: int | None = None,
    holder: str | None = None,
    fingerprints: Mapping[str, str] | None = None,
    system_prompt: str = "you are the harness under evaluation",
    lease_ttl_seconds: float = 1.0,
) -> Deployment:
    """Build the offline deployment a case runs on. Real stores, real gateway, real loop."""
    if profile not in {"edit", "probe"}:
        raise ContractError(f"unknown tool profile {profile!r}")
    state.mkdir(parents=True, exist_ok=True)
    trace = state / "trace.log"
    definitions = (
        [_read_definition(workspace), _write_definition(workspace, trace)]
        if profile == "edit"
        else _probe_definitions(trace, handlers)
    )
    approvals = SqliteApprovalStore(state / "approvals.db")
    gateway = ToolGateway(
        registry=ToolRegistry(definitions),
        policy=ConservativePolicy(auto_write=auto_write),
        approvals=approvals,
        backend=SqliteExecutionBackend(state / "operations.db"),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: _context(attempt))

    def _stream(model: Any, context: Any, options: Any = None) -> Any:
        # The offline adapter ignores the model: it is a script, not a completion.
        return stream(model, context, options)

    harness = DefaultHarness(
        stream_fn=_stream,
        tools=[core_tool(definition, bindings) for definition in definitions],
        persistence=SqlitePersistence(state / "runs.db"),
        system_prompt=system_prompt,
        reconcile=reconcile,
        clock=clock if clock is not None else time.time,
        holder=holder,
        # Short by design: the fault cases recover a run whose worker died, and the run's
        # lease has to expire before another worker may take it. Waiting a second is the real
        # mechanism; production code is never changed to make the fence easier to pass.
        lease_ttl_seconds=lease_ttl_seconds,
        max_runtime_seconds=max_runtime_seconds,
        fingerprints=fingerprints,
    )
    _ = max_turns
    return Deployment(
        harness=harness,
        gateway=gateway,
        approvals=approvals,
        workspace=workspace,
        state=state,
        trace=trace,
    )


def _context(attempt: str) -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="eval", session_id="case", run_id="run-1", attempt_id=attempt
        ),
        principal=Principal(subject="evaluator"),
    )


def case_context(attempt: str) -> RunContext:
    """The run context every case uses (one logical run per case/repeat directory)."""
    return _context(attempt)


def run_coroutine(coro: Any, *, timeout: float = 120.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def run_baseline(spec: FixtureSpec, workspace: Path, *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run the fixture's own baseline command, with argv, a cwd and a bounded output."""
    command = [
        sys.executable if index == 0 and item == PYTHON else item
        for index, item in enumerate(spec.baseline_command)
    ]
    if not command:
        raise ContractError(f"fixture {spec.name!r} declares no baseline command")
    return subprocess.run(
        command,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def trace_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def trace_tools(path: Path) -> list[str]:
    return [line.split(":")[1] for line in trace_lines(path) if len(line.split(":")) > 1]


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
