"""The controlled executor: one case, one isolated directory, one independent verdict.

What the runner is responsible for, and what it deliberately is not:

* it drives the **real harness** with the same Core, the real SQLite stores and the real
  Gateway. There is no second agent loop here, and no in-memory simulation of a crash — the
  crash cases spawn a child process that really exits;
* each case/repeat gets its **own workspace, store directory and identity**, so a run cannot
  inherit another's quota, ledger or transcript. A recovery continues the *same* logical run
  (the same run id, the same stores) rather than starting a fresh, unlimited one;
* it records whether a fault was an **actual process exit** or a simulated one, so a reader
  never has to guess what "crash" meant;
* failures are classified: a framework error is `infra_error`, a case whose premise does not
  hold (the fixture is not in its initial state, the case digest does not match) is
  `invalid_result`, and only an independent verifier's verdict makes something `failed`. The
  three must never be averaged together;
* interrupted or budget-exhausted runs keep whatever the run already produced; nothing is
  retried silently and no quota is reset.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..errors import ContractError
from ..runtime import RunRequest
from ..storage import SqliteApprovalStore, SqliteExecutionBackend, SqlitePersistence
from ..versioning import fingerprint
from .cases import CASES, SUITE, recovery_script_for, script_for
from .fixtures import (
    OFFLINE_MODEL,
    ScriptedStream,
    build_deployment,
    case_context,
    run_baseline,
    run_coroutine,
    trace_lines,
    trace_tools,
    write_fixture,
)
from .types import EvalCase, EvalCaseResult, EvalOutcome, EvalSuite, VerifierOutcome
from .verifiers import VerifierContext, verify

__all__ = ["EvalSuiteResult", "run_case", "run_suite"]


@dataclass
class EvalSuiteResult:
    """Every case result of one suite run, plus the suite it answered."""

    suite: EvalSuite
    results: tuple[EvalCaseResult, ...]
    root: str = ""

    def to_record(self) -> Mapping[str, Any]:
        return {
            "suite": self.suite.to_record(),
            "root": self.root,
            "results": [result.to_record() for result in self.results],
        }


def run_case(
    case: EvalCase,
    root: Path,
    *,
    repeat_index: int = 0,
    model_adapter: Any | None = None,
    allow_real_model: bool = False,
) -> EvalCaseResult:
    """Run one case once, and let its verifier decide the outcome."""
    case_dir = root / case.case_id / f"r{repeat_index}"
    workspace = case_dir / "workspace"
    pristine = case_dir / "pristine"
    state = case_dir / "state"
    attempt = "a1"
    empty = EvalCaseResult(
        case_id=case.case_id,
        case_version=case.version,
        case_digest=case.digest,
        attempt=attempt,
        outcome=EvalOutcome.NOT_RUN,
        repeat_index=repeat_index,
    )
    if case.model != OFFLINE_MODEL:
        # A real-model case is never answered by the scripted adapter, whether or not the host
        # confirmed the run: "I have no adapter" and "I am not allowed to use one" are both
        # `not_run`, and neither is a pass.
        if model_adapter is None:
            return replace(
                empty,
                outcome=EvalOutcome.NOT_RUN,
                notes=(
                    f"case {case.case_id!r} names model {case.model!r} and no adapter was "
                    "configured, so it is reported as not_run rather than answered by the "
                    "scripted offline adapter",
                ),
            )
        if not allow_real_model:
            return replace(
                empty,
                outcome=EvalOutcome.NOT_RUN,
                notes=(
                    f"case {case.case_id!r} names model {case.model!r}; running it against a "
                    "provider requires explicit confirmation",
                ),
            )
    if case.fixture is None:
        return replace(
            empty,
            outcome=EvalOutcome.INVALID_RESULT,
            notes=("the case has no fixture, so there is nothing to run it against",),
        )

    # A fresh, isolated directory per run: no state, no quota and no ledger is inherited.
    if case_dir.exists():
        shutil.rmtree(case_dir)
    write_fixture(case.fixture, workspace)
    write_fixture(case.fixture, pristine)

    # The premise: the fixture must start in the state the case describes (a broken project
    # for the code task). A case whose baseline already passes is not measuring anything.
    if case.fixture.baseline_command:
        baseline = run_baseline(case.fixture, workspace)
        if baseline.returncode == 0:
            return replace(
                empty,
                outcome=EvalOutcome.INVALID_RESULT,
                notes=(
                    "the fixture's baseline already passes, so this case cannot distinguish a "
                    "fixed project from an untouched one",
                ),
                artifacts={"baseline": "passed"},
            )

    started = time.monotonic()
    outcome_status = ""
    error = ""
    model_calls = 0
    human_decisions = 0
    # An adapter answers both halves of a fault case; the offline script has one answer per
    # moment, so the recovery half is selected for the fault cases.
    stream = (
        model_adapter
        if model_adapter is not None
        else ScriptedStream(
            *(script_for(case) if not case.allowed_faults else recovery_script_for(case))
        )
    )
    protocol: dict[str, Any] = {
        "suite": SUITE.name,
        "suite_version": SUITE.version,
        "case": case.case_id,
        "case_version": case.version,
        "model": OFFLINE_MODEL if model_adapter is None else str(case.model),
        "repeats": case.repeats,
        "seed": case.seed,
        "run_id": "run-1",
    }
    try:
        if case.allowed_faults:
            outcome_status, protocol, model_calls = _run_with_fault(
                case, workspace, state, stream, protocol
            )
        else:
            outcome_status, protocol, model_calls, human_decisions = _run_clean(
                case, workspace, state, stream, protocol
            )
    except Exception as exc:  # noqa: BLE001 - a framework failure is its own category
        error = f"{type(exc).__name__}: {exc}"
        return replace(
            empty,
            outcome=EvalOutcome.INFRA_ERROR,
            notes=(f"the framework could not complete the case: {error}",),
            protocol=protocol,
            artifacts={"case_dir": str(case_dir)},
        )

    elapsed = round(time.monotonic() - started, 3)
    context = _verifier_context(case, workspace, pristine, state, outcome_status, error, protocol, stream)
    verdict = verify(case, context)
    protocol["trace"] = list(context.trace)
    protocol["ledger"] = dict(context.ledger)
    if model_calls:
        protocol.setdefault("model_calls", model_calls)
    if verdict.infra_error:
        outcome = EvalOutcome.INFRA_ERROR
    else:
        outcome = EvalOutcome.PASSED if verdict.passed else EvalOutcome.FAILED
    diagnostics = _diagnostics(state, context)
    return EvalCaseResult(
        case_id=case.case_id,
        case_version=case.version,
        case_digest=case.digest,
        attempt=protocol.get("attempt", attempt),
        outcome=outcome,
        run_id=str(protocol.get("run_id") or "run-1"),
        verifier=verdict,
        protocol=protocol,
        stop_code=str(protocol.get("stop_code") or ""),
        error_code=str(protocol.get("error_code") or error),
        final_text=str(protocol.get("final_text") or ""),
        elapsed_seconds=elapsed,
        usage=tuple(diagnostics.get("usage") or ()),
        human_decisions=human_decisions,
        artifacts=_artifacts(workspace, pristine, state, protocol),
        report=diagnostics.get("report"),
        diagnostics={
            "integrity": list(diagnostics.get("integrity") or ()),
            "unsupported": list(diagnostics.get("unsupported") or ()),
        },
        notes=(verdict.infra_error,) if verdict.infra_error else (),
        repeat_index=repeat_index,
    )


def run_suite(
    suite: EvalSuite | None = None,
    root: Path | None = None,
    *,
    case_ids: Sequence[str] | None = None,
    model_adapter: Any | None = None,
    allow_real_model: bool = False,
    model_override: str | None = None,
    repeats: int = 0,
) -> EvalSuiteResult:
    """Run a suite (or a subset of its cases) under one root directory.

    ``model_override`` is how a real-model run is asked for: it replaces each case's model, so
    the cases are *treated as* real-model cases — which, without an adapter, means they are
    reported as ``not_run`` rather than quietly answered by the scripted one.
    """
    suite = suite or SUITE
    base = Path(root) if root is not None else Path("build") / "evals" / suite.name
    selected = [
        case
        for case in suite.cases
        if case_ids is None or case.case_id in set(case_ids)
    ]
    if model_override:
        selected = [
            replace(case, model=model_override, repeats=repeats or max(case.repeats, 2))
            for case in selected
        ]
    results: list[EvalCaseResult] = []
    for case in selected:
        for repeat in range(case.repeats):
            results.append(
                run_case(
                    case,
                    base,
                    repeat_index=repeat,
                    model_adapter=model_adapter,
                    allow_real_model=allow_real_model,
                )
            )
    return EvalSuiteResult(suite=suite, results=tuple(results), root=str(base))


# --- the two driving shapes -------------------------------------------------------------


def _run_clean(
    case: EvalCase,
    workspace: Path,
    state: Path,
    stream: Any,
    protocol: dict[str, Any],
) -> tuple[str, dict[str, Any], int, int]:
    """One process: an approval pause and its resume, or a single uninterrupted run."""
    deployment = build_deployment(
        workspace=workspace,
        state=state,
        stream=stream,
        profile=str(case.tool_config.get("profile") or "edit"),
        auto_write=bool(case.tool_config.get("auto_write")),
        attempt="a1",
        max_runtime_seconds=case.max_runtime_seconds,
    )
    try:
        outcome = run_coroutine(
            deployment.harness.start(
                RunRequest(prompt=case.instructions, max_turns=case.max_turns),
                case_context("a1"),
            )
        )
        protocol["attempts"] = ["a1"]
        protocol["stop_code"] = str(getattr(outcome, "stop_code", "") or "")
        protocol["error_code"] = str(getattr(outcome, "error_code", "") or "")
        protocol["final_text"] = str(getattr(outcome, "final_text", "") or "")
        decisions = 0
        if getattr(outcome, "status", None) is not None and outcome.status.value == "suspended":
            interaction = dict(getattr(outcome, "pending_interaction", None) or {})
            key = str(interaction.get("approval_binding_key") or "")
            if case.expectations.get("approve") and key:
                # The decision is written into the trusted store the gateway reads: a
                # resolution payload is not authorisation, and the evaluator does not pretend
                # it is. The count comes from the store's own history.
                run_coroutine(_approve(state, key))
                decisions = 1
                deployment.workspace = workspace
                resumed = build_deployment(
                    workspace=workspace,
                    state=state,
                    stream=stream,
                    profile=str(case.tool_config.get("profile") or "edit"),
                    auto_write=bool(case.tool_config.get("auto_write")),
                    attempt="a2",
                    max_runtime_seconds=case.max_runtime_seconds,
                )
                try:
                    outcome = run_coroutine(
                        resumed.harness.resume("run-1", {}, case_context("a2"))
                    )
                finally:
                    resumed.close()
                protocol["attempts"] = ["a1", "a2"]
                protocol["stop_code"] = str(getattr(outcome, "stop_code", "") or "")
                protocol["error_code"] = str(getattr(outcome, "error_code", "") or "")
                protocol["final_text"] = str(getattr(outcome, "final_text", "") or "")
        protocol["status"] = str(getattr(getattr(outcome, "status", None), "value", outcome))
        return protocol["status"], protocol, int(getattr(stream, "calls", 0)), decisions
    finally:
        deployment.close()


def _run_with_fault(
    case: EvalCase,
    workspace: Path,
    state: Path,
    stream: Any,
    protocol: dict[str, Any],
) -> tuple[str, dict[str, Any], int]:
    """A real child process dies mid-batch, then this process recovers the run.

    The recovery has two steps on purpose, because that is what the harness guarantees: the
    first resumption classifies every call against the ledger and *pauses* on the one the
    ledger cannot decide (an open intent is not evidence that nothing happened), and the
    second one — with the host's business evidence — converges it and lets the batch finish.
    A case that skipped the pause would be testing a harness this build does not claim to be.
    """
    from .child import run_crash_child

    code, stderr = run_crash_child(case.case_id, state.parent)
    protocol["fault"] = "process-exit"
    protocol["fault_exit_code"] = code
    protocol["fault_window"] = case.allowed_faults[0]
    if code != 9:
        raise ContractError(
            f"the fault injection did not kill the child as declared (exit {code}): {stderr}"
        )
    # The dead worker still holds the run's lease until it expires. The recovering harness
    # arrives *after* that moment rather than bypassing the fence: the store evaluates the
    # claim against the expired lease, exactly as it would in production.
    recovering_clock = lambda: time.time() + 5.0  # noqa: E731 - a documented arrival offset
    resumes: list[str] = []

    if case.case_id == "drift-refusal":
        # One resumption, under a drifted deployment: it must refuse before dispatching.
        changed = build_deployment(
            workspace=workspace,
            state=state,
            stream=stream,
            profile="probe",
            attempt="a2",
            reconcile=_evidence(case),
            system_prompt=str(case.expectations.get("changed_system_prompt") or "changed"),
            clock=recovering_clock,
            lease_ttl_seconds=1.0,
        )
        try:
            run_coroutine(changed.harness.resume("run-1", {}, case_context("a2")))
            protocol["status"] = "completed"
            protocol["error"] = ""
        except Exception as exc:  # noqa: BLE001 - the refusal is the expected answer
            protocol["status"] = "refused"
            protocol["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            changed.close()
        resumes.append("a2")
        protocol["resumes"] = resumes
        protocol["attempts"] = list(resumes)
        protocol["model_calls"] = int(getattr(stream, "calls", 0))
        return protocol["status"], protocol, protocol["model_calls"]

    # Step 1: classify. No reconciler here, so an open intent must pause the batch.
    first = build_deployment(
        workspace=workspace,
        state=state,
        stream=stream,
        profile="probe",
        attempt="a2",
        clock=recovering_clock,
        lease_ttl_seconds=1.0,
    )
    try:
        classified = run_coroutine(first.harness.resume("run-1", {}, case_context("a2")))
    finally:
        first.close()
    resumes.append("a2")
    protocol["classified_status"] = str(
        getattr(getattr(classified, "status", None), "value", classified)
    )
    protocol["classified_interaction"] = dict(
        getattr(classified, "pending_interaction", None) or {}
    )

    # Step 2: the host answers with business evidence, and the batch continues.
    handlers: dict[str, Any] = {}
    if case.case_id == "deadline-during-recovery":
        handlers["probe.c"] = _blocking_probe(3.0)
    second = build_deployment(
        workspace=workspace,
        state=state,
        stream=stream,
        profile="probe",
        attempt="a3",
        handlers=handlers,
        reconcile=_evidence(case),
        max_runtime_seconds=case.max_runtime_seconds,
        clock=recovering_clock,
        lease_ttl_seconds=1.0,
    )
    try:
        outcome = run_coroutine(second.harness.resume("run-1", {}, case_context("a3")))
    finally:
        second.close()
    resumes.append("a3")
    protocol["resumes"] = resumes
    protocol["attempts"] = list(resumes)
    protocol["status"] = str(getattr(getattr(outcome, "status", None), "value", outcome))
    protocol["stop_code"] = str(getattr(outcome, "stop_code", "") or "")
    protocol["error_code"] = str(getattr(outcome, "error_code", "") or "")
    protocol["final_text"] = str(getattr(outcome, "final_text", "") or "")
    return protocol["status"], protocol, int(getattr(stream, "calls", 0))


def _evidence(case: EvalCase) -> Callable[..., Any] | None:
    """The host's business evidence for the probe that was left unproven by the crash."""
    state = str(case.expectations.get("evidence") or "")
    if not state:
        return None

    async def reconciler(call: Any, details: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "proven": True,
            "state": state,
            "detail": "the probe's effect was confirmed by business evidence",
            "source": "eval business evidence",
        }

    return reconciler


def _blocking_probe(seconds: float) -> Callable[..., Any]:
    import asyncio

    async def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        await asyncio.sleep(seconds)
        return {"content": [{"type": "text", "text": "probe.c finished"}]}

    return handler


async def _approve(state: Path, binding_key: str) -> None:
    from ..tools import ApprovalStatus

    approvals = SqliteApprovalStore(state / "approvals.db")
    try:
        await approvals.decide(binding_key, ApprovalStatus.APPROVED, decided_by="eval-operator")
    finally:
        approvals.close()


# --- collecting the evidence ------------------------------------------------------------


def _verifier_context(
    case: EvalCase,
    workspace: Path,
    pristine: Path,
    state: Path,
    outcome_status: str,
    error: str,
    protocol: Mapping[str, Any],
    stream: Any,
) -> VerifierContext:
    return VerifierContext(
        case=case,
        workspace=workspace,
        pristine=pristine,
        state=state,
        run_id=str(protocol.get("run_id") or "run-1"),
        outcome_status=outcome_status,
        error=str(protocol.get("error") or error or ""),
        protocol=protocol,
        workspace_files=_file_texts(workspace),
        pristine_files=_file_texts(pristine),
        ledger=_ledger_states(state),
        trace=tuple(trace_lines(state / "trace.log")),
        model_calls=int(getattr(stream, "calls", 0)),
    )


def _file_texts(directory: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not directory.exists():
        return out
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            out[str(path.relative_to(directory))] = path.read_text(encoding="utf-8", errors="replace")
    return out


def _ledger_states(state: Path) -> dict[str, str]:
    """The ledger's own view, keyed by *tool name* — the name a case's expectations use.

    A verifier asks "did probe.b succeed?", not "what is the state of an operation id the
    run derived?", so it is given the ledger in the vocabulary of the question.
    """
    operations = state / "operations.db"
    if not operations.exists():
        return {}
    ledger = SqliteExecutionBackend(operations, read_only=True)
    try:
        return {str(row["tool_name"]): str(row["state"]) for row in ledger.all_operations()}
    finally:
        ledger.close()


def _diagnostics(state: Path, context: VerifierContext) -> dict[str, Any]:
    """Read the run back through the read-only inspector, for usage and coverage facts."""
    from ..diagnostics.reader import RunInspector
    from ..runtime import RunIdentity

    runs = state / "runs.db"
    if not runs.exists():
        return {}
    operations = state / "operations.db"
    persistence = SqlitePersistence(runs, read_only=True)
    ledger = SqliteExecutionBackend(operations, read_only=True) if operations.exists() else None
    try:
        inspector = RunInspector(persistence=persistence, ledger=ledger)
        report = run_coroutine(
            inspector.inspect_run(
                RunIdentity(
                    namespace="eval", session_id="case", run_id=context.run_id, attempt_id="verifier"
                )
            ),
            timeout=60.0,
        )
    except Exception as exc:  # noqa: BLE001 - a report that cannot be read is reported as such
        return {"integrity": [f"the run could not be read back: {type(exc).__name__}: {exc}"]}
    finally:
        if ledger is not None:
            ledger.close()
        persistence.close()
    return {
        "report": {
            "status": report.status,
            "stop_code": report.stop_code,
            "error_code": report.error_code,
            "origin_attempts": list(report.origin_attempts),
            "boundary": report.boundary.to_record(),
            "source_counts": dict(report.source_counts()),
        },
        "usage": [
            {
                "unit": item.unit,
                "quantity": item.quantity.value,
                "amount": item.amount,
                "source": item.source.value,
                "dedupe_key": item.dedupe_key,
                "estimator": item.estimator,
                "note": item.note,
            }
            for item in report.usage
        ],
        "integrity": list(report.integrity),
        "unsupported": list(report.unsupported),
    }


def _artifacts(
    workspace: Path, pristine: Path, state: Path, protocol: Mapping[str, Any]
) -> dict[str, str]:
    """Digests, not contents: what the run produced, and what it started from."""
    return {
        "workspace": fingerprint(_file_texts(workspace)),
        "pristine": fingerprint(_file_texts(pristine)),
        "trace": fingerprint(list(trace_tools(state / "trace.log"))),
        "protocol": fingerprint({k: str(v) for k, v in protocol.items() if k != "trace"}),
    }


_ = (CASES,)
