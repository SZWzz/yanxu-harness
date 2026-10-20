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

from ..core.types import (
    AssistantMessage,
    StopReason,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
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
    "FAULT_LEASE_TTL_SECONDS",
    "FaultWindow",
    "fault_window",
    "OFFLINE_MODEL",
    "PYTHON",
    "PROBE_TOOLS",
    "RecoveryClock",
    "ScriptedStream",
    "live_lease_expiry",
    "probe_ledger",
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

#: The one lease lifetime every evaluation deployment holds.
#:
#: This used to be three unrelated numbers (1 s for the crashing child, 5 s and 70 s of
#: clock offset for the two recoveries, 60 s for the recovering deployments). A one-second
#: lease was the child's thinnest margin: one commit slower than that on a CI filesystem
#: fails the child's own fence, and because the child then exited with the crash code the
#: fixture reported an injected crash that had not happened. The recoveries still arrive
#: *after* the previous worker's lease has expired — read from the store, not assumed — so
#: the fence is exercised rather than bypassed.
FAULT_LEASE_TTL_SECONDS = 60.0

#: How far past a lease's real expiry a recovering process arrives. Small on purpose: the
#: point is "after expiry", not "long after expiry", and waiting in wall-clock time would
#: be a slower test of the same fact.
LEASE_ARRIVAL_TICK_SECONDS = 0.001

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

_REPORT = '"""A tiny module with one defect the task has to locate."""\n\n\ndef average(values):\n    """Return the mean of ``values``."""\n    total = 0\n    for value in values:\n        total += value\n    return total / len(values) + 1\n\n\ndef spread(values):\n    """Return the difference between the largest and smallest value."""\n    return max(values) - min(values)\n'

_BUGGY_TASK = '# Task\n\n`report.py` has exactly one defect. Report which function contains it and quote the exact\nline that is wrong. Nothing may be changed: this is a *location* task, and an answer that\nonly says the work is done answers nothing.\n'

_CHECKS = 'from report import average, spread\n\n\ndef test_average_of_three():\n    assert average([1, 2, 3]) == 2\n\n\ndef test_spread():\n    assert spread([4, 9]) == 5\n'

_CHECK_TASK = "# Task\n\nA change was made to `calc.py` and left for review. Run the project's own tests, then\nreview the change: say whether the tests actually pass (report the real result, not what\nyou expect) and name the defect the change introduces, with the file and the exact line.\n"

_PATCH_DIFF = '--- a/calc.py\n+++ b/calc.py\n@@ -6,7 +6,7 @@\n-    for value in values:\n+    for value in values[:-1]:\n         result += value\n'


FIXTURES: Mapping[str, FixtureSpec] = {
    "calc": FixtureSpec(
        name="calc",
        version="1",
        files={"calc.py": _CALC, "test_calc.py": _CALC_TEST, "task.md": _TASK},
        # The fixture is *supposed* to be broken; a case whose baseline already passes is not
        # measuring anything, so the runner checks this first.
        baseline_command=(PYTHON, "-m", "pytest", "test_calc.py", "-q"),
    ),
    "buggy": FixtureSpec(
        name="buggy",
        version="1",
        files={"report.py": _REPORT, "checks.py": _CHECKS, "task.md": _BUGGY_TASK},
    ),
    "review": FixtureSpec(
        name="review",
        version="1",
        files={
            "calc.py": _CALC,
            "test_calc.py": _CALC_TEST,
            "patch.diff": _PATCH_DIFF,
            "task.md": _CHECK_TASK,
        },
        # The check the review is about: the change makes the project's own tests fail. The
        # verifier runs this command itself instead of trusting what the review reports.
        baseline_command=(PYTHON, "-m", "pytest", "test_calc.py", "-q"),
    ),
    "notes": FixtureSpec(
        name="notes",
        version="1",
        files={"notes.md": "", "task.md": "# Task\n\nWrite the requested line into notes.md.\n"},
    ),
}


def long_history(turns: int = 8, *, newest: str = "") -> list[Any]:
    """A transcript long enough that the projection has to drop something.

    Built here rather than in a test module because a case must be reproducible from the
    installed package alone. Every message is deterministic: the same case always produces the
    same history, and therefore the same projection decision.
    """
    history: list[Any] = []
    for index in range(turns):
        history.append(UserMessage(content=f"question {index} " * 24))
        history.append(
            AssistantMessage(
                content=[ToolCall(id=f"c{index}", name="read_file", arguments={"path": "task.md"})],
                stop_reason=StopReason.TOOL_USE,
            )
        )
        history.append(
            ToolResultMessage(
                tool_call_id=f"c{index}",
                tool_name="read_file",
                content=[TextContent(text=f"result {index} " * 24)],
            )
        )
    history.append(UserMessage(content=newest or "the final question"))
    return history


def real_converter(messages: Sequence[Any]) -> Any:
    """The conversion the wire path uses, so a projection is measured against the real request."""
    from ..core.messages import normalize_context, to_llm_messages
    from ..core.types import AgentContext
    from ..providers.core_bridge import to_provider_request

    llm = to_llm_messages(list(messages))
    return to_provider_request(None, normalize_context(AgentContext(messages=llm)))


def blocking_summarizer(*, entered: Any | None = None) -> Any:
    """A summarizer that enters and then waits: the run's deadline is what ends it.

    It announces its entry through an :class:`asyncio.Event` when one is given, so a test can
    prove the cancellation happened *inside* the auxiliary call rather than before it.
    """

    class Blocking:
        def __init__(self) -> None:
            self.calls = 0

        async def summarize(self, messages: Sequence[Any], *, scope: str, source_head: str) -> Any:
            from ..context import SummaryResult

            self.calls += 1
            if entered is not None:
                entered.set()
            await asyncio.Event().wait()  # never set: the run's deadline must stop this
            return SummaryResult(text="unreachable", scope=scope, source_head=source_head)

    return Blocking()


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


# --- the crash window a fault case claims ------------------------------------------------


def probe_ledger(state: Path) -> Mapping[str, Mapping[str, Any]]:
    """The ledger's rows keyed by tool name, with their intent and receipt timestamps.

    Read-only: the evaluation asks the ledger what happened, and never promotes a row to
    make a recovery look like it worked.
    """
    operations = state / "operations.db"
    if not operations.exists():
        return {}
    ledger = SqliteExecutionBackend(operations, read_only=True)
    try:
        return {str(row["tool_name"]): dict(row) for row in ledger.all_operations()}
    finally:
        ledger.close()


def live_lease_expiry(state: Path, *, resource: str = "run:run-1") -> float | None:
    """The store's own expiry for a lease that is still held, or ``None``.

    The crashed child never releases its lease, so the run's row is still live when the parent
    recovers. Reading the expiry from the store is what lets the recovering deployment arrive
    *after* the real deadline without a number like ``5.0`` or ``70.0`` standing in for it,
    and without either bypassing the fence or sleeping through it. A released lease leaves an
    empty token and a zero expiry behind, which is reported as ``None``.
    """
    import sqlite3

    path = state / "runs.db"
    if not path.exists():
        return None
    connection = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False
    )
    try:
        row = connection.execute(
            "SELECT token, expires_at FROM leases WHERE resource = ?", (resource,)
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row[0]:
        return None
    return float(row[1])


class RecoveryClock:
    """A clock that arrives after the previous worker's real lease expiry, and never rewinds.

    A dead worker's lease is a durable fact; the recovering process has to arrive after it.
    Each recovery step calls :meth:`step_past` with the expiry it read from the store, so the
    store's own fence decides whether the claim is allowed — nothing here weakens the
    holder/expiry check, it only supplies the moment. Between steps the clock advances with
    real time, so a run that takes a little while is not silently given a second budget.
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._floor = 0.0
        self._origin = now()

    def step_past(self, expires_at: float) -> None:
        """Arrive just after ``expires_at`` (which came from the store, not from a constant)."""
        self._floor = max(self._floor, float(expires_at) + LEASE_ARRIVAL_TICK_SECONDS)
        self._origin = self._now()

    def __call__(self) -> float:
        if self._floor <= 0.0:
            return self._now()
        return max(self._floor, self._floor + (self._now() - self._origin))


@dataclass(frozen=True)
class FaultWindow:
    """What a ``partial-batch-then-die`` fixture claims its crash left behind.

    The exit code alone cannot establish this. A child that dies for any other reason also
    leaves *a* process exit behind, and treating that as the injected crash is how a recovery
    case ends up measuring a window it never produced. These three facts are the window:

    * the first probe **settled** (its effect is committed and durable);
    * the second probe has an **intent with no receipt** (it was admitted, and its handler was
      entered — the trace line is written by that handler immediately before the process
      dies — so its effect is genuinely unproven);
    * the third probe was **never admitted** (no ledger row at all).
    """

    rows: Mapping[str, Mapping[str, Any]]
    trace: tuple[str, ...]
    probes: tuple[str, ...] = PROBE_TOOLS

    @property
    def problems(self) -> tuple[str, ...]:
        if len(self.probes) != 3:
            return (
                "the partial-batch window is defined over exactly three probes, not "
                f"{self.probes!r}",
            )
        settled, open_intent, never = self.probes
        ran = [line.split(":")[1] for line in self.trace if line.count(":") >= 2]
        problems: list[str] = []
        if ran.count(settled) != 1:
            problems.append(f"{settled} ran {ran.count(settled)} time(s), expected exactly one")
        if ran.count(open_intent) != 1:
            problems.append(
                f"{open_intent} ran {ran.count(open_intent)} time(s), so the crash did not happen "
                "inside its handler"
            )
        if ran.count(never):
            problems.append(f"{never} ran even though it was never admitted")

        first = self.rows.get(settled)
        if first is None:
            problems.append(f"the ledger holds no operation for {settled}")
        elif first.get("state") != "succeeded" or first.get("settled_at") is None:
            problems.append(
                f"{settled} is {first.get('state')!r} with "
                f"settled_at={first.get('settled_at')!r}, not a settled effect"
            )

        second = self.rows.get(open_intent)
        if second is None:
            problems.append(
                f"the ledger holds no admission/intent for {open_intent}, so the crash window "
                "is not the one the case declares"
            )
        elif second.get("settled_at") is not None or second.get("state") not in {
            "queued",
            "running",
        }:
            problems.append(
                f"{open_intent} is {second.get('state')!r} with "
                f"settled_at={second.get('settled_at')!r}; the window needs an open intent "
                "with no receipt"
            )

        if never in self.rows:
            problems.append(
                f"the ledger already holds {never} ({self.rows[never].get('state')!r}); it was "
                "supposed to be admitted for the first time by the recovery"
            )
        return tuple(problems)

    @property
    def satisfied(self) -> bool:
        return not self.problems

    def to_record(self) -> Mapping[str, Any]:
        return {
            "window": "partial-batch-then-die",
            "probes": list(self.probes),
            "satisfied": self.satisfied,
            "problems": list(self.problems),
            # State and "has a receipt", not wall-clock epochs: the record says which window
            # was proven without pinning the file to the moment it was proven.
            "ledger": {
                name: {
                    "state": row.get("state"),
                    "has_receipt": row.get("settled_at") is not None,
                }
                for name, row in self.rows.items()
            },
            "trace": list(self.trace),
        }


def fault_window(state: Path, *, probes: Sequence[str] = PROBE_TOOLS) -> FaultWindow:
    """The window the crashed child actually left behind, read from the real stores."""
    return FaultWindow(
        rows=probe_ledger(state),
        trace=tuple(trace_lines(state / "trace.log")),
        probes=tuple(probes),
    )


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
    backend: SqliteExecutionBackend
    persistence: SqlitePersistence
    workspace: Path
    state: Path
    trace: Path

    def close(self) -> None:
        try:
            self.persistence.close()
        finally:
            try:
                self.backend.close()
            finally:
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


def _tests_definition(
    workspace: Path, fixture: FixtureSpec, trace: Path, policy: Any | None = None
) -> ToolDefinition:
    """Run the fixture's own check command and hand the model its real result.

    The evaluation's smallest *tool* adapter: argv, an explicit cwd, a timeout and a bounded
    output, called through the Gateway like any other tool. It exists because a review task is
    about what the project's tests actually do — a review that only asserts "tests pass" has
    measured nothing. The output is the real command's, with host paths replaced.
    """
    from .verifiers import summarise_command_output

    def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        completed = run_baseline(fixture, workspace, policy=policy)
        summary = summarise_command_output(
            (completed.stdout or "") + (completed.stderr or ""), workspace
        )
        passed = completed.returncode == 0
        # The exit code is recorded in the same append-only trace as every other effect, so the
        # verifier can compare a review's *claim* with the real result without trusting either
        # the model-visible prose or the tool's own details projection.
        with trace.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{context.identity.attempt_id}:run_tests:exit={completed.returncode}\n"
            )
        return {
            "content": [
                TextContent(
                    text=(
                        f"the project's own tests exited {completed.returncode} "
                        f"({'pass' if passed else 'fail'})\n{summary}"
                    )
                )
            ],
            "details": {
                # The machine-readable half, so a verifier can compare a *claim* with the real
                # exit code instead of parsing prose.
                "status": "executed",
                "exit_code": completed.returncode,
                "tests_passed": passed,
            },
        }

    return ToolDefinition(
        "run_tests",
        "run the fixture's own check command inside the workspace",
        # A read effect: it runs the project's tests, it does not change the workspace. Stated
        # explicitly so the deployment's policy treats it as the read it is.
        effects=frozenset({ToolEffect.READ}),
        args_validator=lambda arguments: {},
        handler=handler,
    )


def _read_definition(workspace: Path, trace: Path) -> ToolDefinition:
    def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        target = workspace / str(arguments["path"])
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        # Reads are traced too, and for the same reason writes are: "the run actually read the
        # file the defect is in" must be checkable independently of what the answer claims.
        with trace.open("a", encoding="utf-8") as handle:
            handle.write(f"{context.identity.attempt_id}:read_file:{arguments['path']}\n")
        return {
            "content": [TextContent(text=text)],
            "details": {"bytes": len(text.encode("utf-8")), "status": "executed"},
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
    lease_ttl_seconds: float = FAULT_LEASE_TTL_SECONDS,
    fixture: FixtureSpec | None = None,
    context_controller: Any | None = None,
    command_policy: Any | None = None,
) -> Deployment:
    """Build the offline deployment a case runs on. Real stores, real gateway, real loop."""
    if profile not in {"edit", "probe", "review"}:
        raise ContractError(f"unknown tool profile {profile!r}")
    state.mkdir(parents=True, exist_ok=True)
    trace = state / "trace.log"
    if profile == "review" and fixture is None:
        raise ContractError("the review profile needs the case's fixture to know what to run")
    if profile == "edit":
        definitions = [_read_definition(workspace, trace), _write_definition(workspace, trace)]
    elif profile == "review":
        definitions = [
            _read_definition(workspace, trace),
            _tests_definition(workspace, fixture, trace, policy=command_policy),
            _write_definition(workspace, trace),
        ]
    else:
        definitions = _probe_definitions(trace, handlers)
    approvals = SqliteApprovalStore(state / "approvals.db")
    backend = SqliteExecutionBackend(state / "operations.db")
    persistence = SqlitePersistence(state / "runs.db")
    gateway = ToolGateway(
        registry=ToolRegistry(definitions),
        policy=ConservativePolicy(auto_write=auto_write),
        approvals=approvals,
        backend=backend,
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: _context(attempt))

    def _stream(model: Any, context: Any, options: Any = None) -> Any:
        # The offline adapter ignores the model: it is a script, not a completion.
        return stream(model, context, options)

    harness = DefaultHarness(
        stream_fn=_stream,
        tools=[core_tool(definition, bindings) for definition in definitions],
        persistence=persistence,
        system_prompt=system_prompt,
        reconcile=reconcile,
        clock=clock if clock is not None else time.time,
        holder=holder,
        # Eval deployments do durable SQLite work and need enough lease time on slower CI
        # filesystems. Fault cases still prove the fence: their crashed child holds the *same*
        # lifetime, and the recovery deployments arrive after the expiry the store recorded —
        # read from the store, never assumed.
        lease_ttl_seconds=lease_ttl_seconds,
        max_runtime_seconds=max_runtime_seconds,
        fingerprints=fingerprints,
        context_controller=context_controller,
    )
    _ = max_turns
    return Deployment(
        harness=harness,
        gateway=gateway,
        approvals=approvals,
        backend=backend,
        persistence=persistence,
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


def run_baseline(
    spec: FixtureSpec,
    workspace: Path,
    *,
    timeout: float | None = None,
    policy: Any | None = None,
) -> Any:
    """Run the fixture's own baseline command under the case's policy.

    ``timeout`` is the caller's remaining budget, not the only limit: the effective deadline is
    the smaller of it and the policy's timeout, and the output is bounded while it is read
    (:mod:`yanxu_harness.evals.commands`). The result is a ``CommandResult``, not a
    ``subprocess.CompletedProcess``, because "how much was dropped" and "was the cleanup proven"
    have to be facts a caller can read rather than things it infers from a trimmed string.
    """
    from .commands import DEFAULT_COMMAND_POLICY, run_command

    command = [
        sys.executable if index == 0 and item == PYTHON else item
        for index, item in enumerate(spec.baseline_command)
    ]
    if not command:
        raise ContractError(f"fixture {spec.name!r} declares no baseline command")
    effective = policy or DEFAULT_COMMAND_POLICY
    return run_command(command, cwd=workspace, policy=effective, deadline_seconds=timeout)


def trace_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def trace_tools(path: Path) -> list[str]:
    return [line.split(":")[1] for line in trace_lines(path) if len(line.split(":")) > 1]


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
