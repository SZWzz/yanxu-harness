"""Frozen deterministic evaluations for correlated diagnostic facts.

These cases exercise the timeline read protocol with immutable source fixtures and the same
``EvalCase``/``EvalSuite``/independent-verifier contracts as the harness suite. They make no
provider request and do not simulate a model's task quality; real-model evaluations remain a
separate, explicitly configured run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..diagnostics import (
    MetricCoverage,
    MetricName,
    MetricObservation,
    RootCauseCategory,
    SourceKind,
    TimelineFact,
    TimelineInspector,
    TimelineSourcePage,
)
from .types import EvalCase, EvalCaseResult, EvalOutcome, EvalSuite, FixtureSpec
from .verifiers import VerifierContext, verifier_digest, verify

__all__ = [
    "TIMELINE_CASES",
    "TIMELINE_CASES_VERSION",
    "TIMELINE_SUITE",
    "timeline_case_by_id",
    "run_timeline_case",
    "run_timeline_suite",
]

TIMELINE_CASES_VERSION = "1"
_VERIFIER = "timeline-protocol"


def _row(
    source_id: str,
    reference: str,
    kind: str,
    sequence: int | None,
    *,
    status: str = "",
    source_cursor: str | int | None = None,
    related: Sequence[str] = (),
    causal: Sequence[str] = (),
    root_cause: str | None = None,
    metric: str | None = None,
    metric_value: float | None = None,
    metric_coverage: str = "complete",
    metric_clock_domain: str = "",
    endpoints_same_clock: bool = False,
) -> Mapping[str, Any]:
    metrics = ()
    if metric:
        metrics = (
            {
                "name": metric,
                "value": metric_value,
                "unit": "count" if metric.endswith("count") or metric in {"accepted", "rejected", "inconclusive"} else "seconds",
                "coverage": metric_coverage,
                "clock_domain": metric_clock_domain,
                "endpoints_same_clock": endpoints_same_clock,
            },
        )
    return {
        "source_id": source_id,
        "reference": reference,
        "kind": kind,
        "source_kind": "receipt" if kind == "operation_receipt" else "committed",
        "sequence": sequence,
        "source_cursor": sequence if source_cursor is None and sequence is not None else (source_cursor or reference),
        "related_references": list(related),
        "causal_references": list(causal),
        "status": status,
        "summary": "",
        "display_at": None,
        "metrics": list(metrics),
        "root_cause": root_cause,
    }


def _fixture(sources: Mapping[str, Sequence[Mapping[str, Any]]]) -> FixtureSpec:
    return FixtureSpec(
        name="timeline-protocol",
        version=TIMELINE_CASES_VERSION,
        files={
            "timeline.json": json.dumps(
                {key: list(value) for key, value in sorted(sources.items())},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        },
    )


def _case(
    case_id: str,
    *,
    instructions: str,
    sources: Mapping[str, Sequence[Mapping[str, Any]]],
    expectations: Mapping[str, Any],
    fault_position: str,
    initial_state: str,
    budget: Mapping[str, Any],
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        version=TIMELINE_CASES_VERSION,
        instructions=instructions,
        instructions_ref=f"fixture:timeline/{case_id}.md",
        fixture=_fixture(sources),
        tool_config={"policy": "read-only-timeline-v1", "runner": "deterministic-fixture"},
        verifier=_VERIFIER,
        verifier_digest=verifier_digest(_VERIFIER),
        max_turns=1,
        model="offline",
        repeats=1,
        expectations={
            **dict(expectations),
            "initial_state": initial_state,
            "fault_position": fault_position,
            "budget": dict(budget),
            "policy": "read-only-timeline-v1",
        },
    )


TIMELINE_CASES: tuple[EvalCase, ...] = (
    _case(
        "timeline-normal-multi-step",
        instructions="Correlate a completed three-step Plan through its task, request, receipt, approval and acceptance facts.",
        sources={
            "plan": [_row("plan", "binding:step-1", "plan_binding", 1, status="bound")],
            "task": [_row("task", "task:attempt-1", "task_attempt", 1, causal=("binding:step-1",), status="completed")],
            "run": [_row("run", "run:request-1", "model_request_dispatched", 1, causal=("task:attempt-1",), status="sent")],
            "operation": [_row("operation", "operation:receipt-1", "operation_receipt", None, status="settled", related=("run:request-1",), causal=("run:request-1",))],
            "approval": [_row("approval", "approval:decision-1", "approval_decision", 1, status="approved")],
            "acceptance": [_row("acceptance", "acceptance:result-1", "acceptance", 1, status="accepted", causal=("operation:receipt-1",))],
        },
        expectations={
            "required_references": ["binding:step-1", "task:attempt-1", "run:request-1", "operation:receipt-1", "approval:decision-1", "acceptance:result-1"],
            "forbidden_references": [],
            "required_gaps": ["sequence_unavailable"],
            "root_causes": [],
            "counts": {"model_request_dispatched|sent": 1, "operation_receipt|settled": 1, "acceptance|accepted": 1},
            "related_pairs": [["operation:receipt-1", "run:request-1"]],
        },
        fault_position="none",
        initial_state="Plan revision 1 is approved; one bound Task and Run have a settled receipt.",
        budget={"provider_requests": 0, "max_source_facts": 12},
    ),
    _case(
        "timeline-last-quota-contention",
        instructions="Verify that two concurrent claims for the last quota unit produced one admission and one refusal.",
        sources={
            "budget": [
                _row("budget", "reservation:winner", "budget_reservation", 1, status="accepted"),
                _row("budget", "reservation:loser", "budget_reservation", 2, status="rejected", root_cause="budget"),
            ],
            "run": [_row("run", "request:winner", "model_request_dispatched", 1, causal=("reservation:winner",), status="sent")],
        },
        expectations={
            "required_references": ["reservation:winner", "reservation:loser", "request:winner"],
            "forbidden_references": ["request:loser"],
            "required_gaps": [],
            "root_causes": ["budget"],
            "counts": {"model_request_dispatched|sent": 1, "budget_reservation|accepted": 1, "budget_reservation|rejected": 1},
        },
        fault_position="parallel claim at last quota unit",
        initial_state="One quota unit remains; two claimants start from the same durable budget snapshot.",
        budget={"quota_units": 1, "provider_requests": 0, "max_source_facts": 8},
    ),
    _case(
        "timeline-approval-resource-drift",
        instructions="Confirm that resource drift after approval refused a new request before dispatch.",
        sources={
            "approval": [_row("approval", "approval:approved-1", "approval_decision", 1, status="approved")],
            "deployment": [_row("deployment", "deployment:drift-refusal", "deployment_drift_refusal", 1, status="refused", root_cause="deployment_drift")],
            "run": [],
        },
        expectations={
            "required_references": ["approval:approved-1", "deployment:drift-refusal"],
            "forbidden_references": ["request:after-drift"],
            "required_gaps": [],
            "root_causes": ["deployment_drift"],
            "counts": {"model_request_dispatched|sent": 0, "deployment_drift_refusal|refused": 1},
        },
        fault_position="resource version changes after approval and before dispatch",
        initial_state="Approval covers revision 1; the host now resolves a different workspace version.",
        budget={"provider_requests": 0, "max_source_facts": 8},
    ),
    _case(
        "timeline-partial-side-effect-crash",
        instructions="Show one settled external receipt and one unknown operation after a worker crash without inventing a second effect.",
        sources={
            "run": [
                _row("run", "run:tool-call-1", "tool_call_dispatched", 1, status="observed"),
                _row("run", "run:tool-call-2", "tool_call_dispatched", 2, status="unknown"),
            ],
            "operation": [
                _row("operation", "operation:receipt-1", "operation_receipt", None, status="settled", related=("run:tool-call-1",), causal=("run:tool-call-1",)),
                _row("operation", "operation:unknown-2", "operation_unknown", None, status="unknown", related=("run:tool-call-2",), causal=("run:tool-call-2",), root_cause="operation_unknown"),
            ],
        },
        expectations={
            "required_references": ["run:tool-call-1", "run:tool-call-2", "operation:receipt-1", "operation:unknown-2"],
            "forbidden_references": ["operation:receipt-2"],
            "required_gaps": ["sequence_unavailable"],
            "root_causes": ["operation_unknown"],
            "counts": {"operation_receipt|settled": 1, "operation_unknown|unknown": 1, "tool_call_dispatched|observed": 1, "tool_call_dispatched|unknown": 1},
            "related_pairs": [["operation:receipt-1", "run:tool-call-1"], ["operation:unknown-2", "run:tool-call-2"]],
        },
        fault_position="process exits after the second handler starts and before its receipt commits",
        initial_state="First effect has a durable receipt; second operation is admitted with no receipt.",
        budget={"external_effects": 2, "provider_requests": 0, "max_source_facts": 8},
    ),
    _case(
        "timeline-stale-dependency-context",
        instructions="Refuse a model request when a required dependency version changed after its context manifest was prepared.",
        sources={
            "context": [_row("context", "context:manifest-1", "context_material", 1, status="stale", root_cause="context")],
            "run": [],
        },
        expectations={
            "required_references": ["context:manifest-1"],
            "forbidden_references": ["request:stale-context"],
            "required_gaps": [],
            "root_causes": ["context"],
            "counts": {"model_request_dispatched|sent": 0, "context_material|stale": 1},
        },
        fault_position="dependency changes after manifest creation and before request dispatch",
        initial_state="Step manifest records dependency revision 4; authoritative dependency is revision 5.",
        budget={"provider_requests": 0, "max_source_facts": 6},
    ),
    _case(
        "timeline-rejection-replanning",
        instructions="Keep a rejected revision's binding separate from a fresh binding after the Plan is replanned.",
        sources={
            "plan": [
                _row("plan", "binding:revision-1", "plan_binding", 1, status="rejected"),
                _row("plan", "binding:revision-2", "plan_binding", 2, status="bound"),
            ],
            "acceptance": [
                _row("acceptance", "acceptance:rejected-1", "acceptance", 1, status="rejected", causal=("binding:revision-1",), root_cause="acceptance"),
                _row("acceptance", "acceptance:accepted-2", "acceptance", 2, status="accepted", causal=("binding:revision-2",)),
            ],
            "run": [_row("run", "run:revision-2-request", "model_request_dispatched", 1, causal=("binding:revision-2",), status="sent")],
        },
        expectations={
            "required_references": ["binding:revision-1", "binding:revision-2", "acceptance:rejected-1", "acceptance:accepted-2", "run:revision-2-request"],
            "forbidden_references": ["run:revision-1-request"],
            "required_gaps": [],
            "root_causes": ["acceptance"],
            "counts": {"acceptance|rejected": 1, "acceptance|accepted": 1, "model_request_dispatched|sent": 1},
        },
        fault_position="acceptance rejection triggers replan before a new binding is admitted",
        initial_state="Revision 1 binding is rejected; revision 2 has no binding, attempt or Run.",
        budget={"provider_requests": 1, "max_source_facts": 10},
    ),
)

TIMELINE_SUITE = EvalSuite(
    name="harness-diagnostic-timeline",
    version=TIMELINE_CASES_VERSION,
    cases=TIMELINE_CASES,
)


class _FixtureSource:
    def __init__(self, source_id: str, rows: Sequence[TimelineFact]) -> None:
        self.source_id = source_id
        self.rows = tuple(rows)
        self.sequence_ordered = all(row.sequence is not None for row in self.rows)
        self.read_only = True

    async def capture_watermark(self, identity: Any) -> int | str:
        if self.sequence_ordered:
            return max((int(row.sequence or 0) for row in self.rows), default=-1)
        return "fixture-snapshot-v1"

    async def read_page(
        self,
        identity: Any,
        *,
        cursor: str | int | None,
        watermark: str | int,
        limit: int,
    ) -> TimelineSourcePage:
        if self.sequence_ordered:
            after = -1 if cursor is None else int(cursor)
            selected = sorted(
                (row for row in self.rows if after < int(row.sequence or 0) <= int(watermark)),
                key=lambda row: int(row.sequence or 0),
            )
        else:
            after_ref = "" if cursor is None else str(cursor)
            selected = sorted((row for row in self.rows if row.reference > after_ref), key=lambda row: row.reference)
        page = selected[:limit]
        return TimelineSourcePage(facts=tuple(page), exhausted=len(selected) <= limit)


def _fact_from_record(record: Mapping[str, Any]) -> TimelineFact:
    metrics = tuple(
        MetricObservation(
            name=MetricName(item["name"]),
            value=item.get("value"),
            unit=str(item["unit"]),
            coverage=MetricCoverage(item.get("coverage", "complete")),
            clock_domain=str(item.get("clock_domain") or ""),
            endpoints_same_clock=bool(item.get("endpoints_same_clock")),
        )
        for item in record.get("metrics") or ()
    )
    return TimelineFact(
        source_id=str(record["source_id"]),
        reference=str(record["reference"]),
        kind=str(record["kind"]),
        source_kind=SourceKind(record["source_kind"]),
        sequence=record.get("sequence"),
        source_cursor=record["source_cursor"],
        related_references=tuple(record.get("related_references") or ()),
        causal_references=tuple(record.get("causal_references") or ()),
        status=str(record.get("status") or ""),
        summary=str(record.get("summary") or ""),
        display_at=record.get("display_at"),
        metrics=metrics,
        root_cause=record.get("root_cause"),
    )


def timeline_case_by_id(case_id: str) -> EvalCase:
    for case in TIMELINE_CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(case_id)


def run_timeline_case(case: EvalCase) -> EvalCaseResult:
    """Run one frozen source fixture and apply its independent verifier."""
    if case.model != "offline":
        return EvalCaseResult(
            case_id=case.case_id,
            case_version=case.version,
            case_digest=case.digest,
            case_digest_algorithm=case.digest_algorithm,
            attempt="offline-1",
            outcome=EvalOutcome.NOT_RUN,
            notes=("real-model timeline evaluations require a configured adapter and explicit run authorization",),
        )
    if case.fixture is None or "timeline.json" not in case.fixture.files:
        return EvalCaseResult(
            case_id=case.case_id,
            case_version=case.version,
            case_digest=case.digest,
            case_digest_algorithm=case.digest_algorithm,
            attempt="offline-1",
            outcome=EvalOutcome.INVALID_RESULT,
            notes=("the frozen timeline case has no source fixture",),
        )
    try:
        raw_sources = json.loads(case.fixture.files["timeline.json"])
        sources = {
            source_id: _FixtureSource(
                source_id,
                tuple(_fact_from_record(row) for row in rows),
            )
            for source_id, rows in raw_sources.items()
        }
        identities = {source_id: source_id for source_id in sources}
        report = asyncio.run(TimelineInspector(tuple(sources.values())).inspect_page(identities, limit=100))
    except Exception as exc:
        return EvalCaseResult(
            case_id=case.case_id,
            case_version=case.version,
            case_digest=case.digest,
            case_digest_algorithm=case.digest_algorithm,
            attempt="offline-1",
            outcome=EvalOutcome.INFRA_ERROR,
            notes=(f"the timeline fixture could not be read ({type(exc).__name__})",),
        )
    protocol = {
        "case_digest": case.digest,
        "fixture_digest": case.fixture.digest,
        "provider_requests": 0,
        "case_contract": {
            "policy": case.expectations.get("policy"),
            "budget": dict(case.expectations.get("budget") or {}),
            "fault_position": case.expectations.get("fault_position"),
            "initial_state": case.expectations.get("initial_state"),
        },
        "timeline_report": report.to_record(),
    }
    context = VerifierContext(
        case=case,
        workspace=Path("."),
        pristine=Path("."),
        state=Path("."),
        run_id=f"eval:{case.case_id}",
        outcome_status="completed",
        protocol=protocol,
    )
    verdict = verify(case, context)
    outcome = (
        EvalOutcome.INFRA_ERROR
        if verdict.infra_error
        else EvalOutcome.PASSED if verdict.passed else EvalOutcome.FAILED
    )
    return EvalCaseResult(
        case_id=case.case_id,
        case_version=case.version,
        case_digest=case.digest,
        case_digest_algorithm=case.digest_algorithm,
        attempt="offline-1",
        outcome=outcome,
        verifier=verdict,
        protocol=protocol,
        report=report.to_record(),
        artifacts={"fixture_digest": case.fixture.digest},
        notes=(verdict.infra_error,) if verdict.infra_error else (),
    )


def run_timeline_suite(
    suite: EvalSuite = TIMELINE_SUITE,
    *,
    case_ids: Sequence[str] | None = None,
) -> Any:
    """Run selected deterministic timeline cases with the shared EvalSuite result shape."""
    from .runner import EvalSuiteResult

    selected = [case for case in suite.cases if case_ids is None or case.case_id in set(case_ids)]
    return EvalSuiteResult(suite=suite, results=tuple(run_timeline_case(case) for case in selected))
