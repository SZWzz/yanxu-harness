from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from yanxu_harness.diagnostics import (
    CoverageGapCode,
    MetricCoverage,
    MetricName,
    MetricObservation,
    RootCauseCategory,
    SourceKind,
    TimelineFact,
    TimelineInspector,
    TimelineSourcePage,
    PlanInspector,
    timeline_report_to_json,
    timeline_report_to_markdown,
)
from yanxu_harness.errors import ConfigurationError, ContractError


@dataclass
class _Source:
    source_id: str
    rows: list[TimelineFact]
    sequence_ordered: bool = True
    read_only: bool = True
    truncate: bool = False
    fail_watermark: bool = False
    calls: list[tuple[object, object, int]] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def capture_watermark(self, identity: object) -> int | str:
        if self.fail_watermark:
            raise OSError("unavailable")
        if not self.sequence_ordered:
            return "snapshot-1"
        return max((fact.sequence or 0 for fact in self.rows), default=-1)

    async def read_page(
        self,
        identity: object,
        *,
        cursor: str | int | None,
        watermark: str | int,
        limit: int,
    ) -> TimelineSourcePage:
        assert self.calls is not None
        self.calls.append((cursor, watermark, limit))
        if self.sequence_ordered:
            after = -1 if cursor is None else int(cursor)
            matching = [
                fact for fact in self.rows
                if fact.sequence is not None and after < fact.sequence <= int(watermark)
            ]
            matching.sort(key=lambda fact: fact.sequence or 0)
        else:
            matching = sorted(self.rows, key=lambda fact: fact.reference)
        selected = matching[:limit]
        return TimelineSourcePage(
            facts=tuple(selected),
            exhausted=len(matching) <= limit,
            truncated=self.truncate,
        )


def _fact(
    source: str,
    reference: str,
    sequence: int | None,
    *,
    source_cursor: int | str | None = None,
    causal: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    display_at: float | None = None,
    summary: str = "",
    kind: str = "record",
    status: str = "",
    metrics: tuple[MetricObservation, ...] = (),
    root_cause: RootCauseCategory | None = None,
) -> TimelineFact:
    return TimelineFact(
        source_id=source,
        reference=reference,
        kind=kind,
        source_kind=SourceKind.COMMITTED,
        sequence=sequence,
        source_cursor=(sequence if source_cursor is None else source_cursor),
        causal_references=causal,
        related_references=related,
        display_at=display_at,
        summary=summary,
        status=status,
        metrics=metrics,
        root_cause=root_cause,
    )


def test_fixed_watermark_cursor_vector_pages_without_skipping_or_reordering_sources() -> None:
    plan = _Source("plan", [_fact("plan", "plan-1", 1), _fact("plan", "plan-2", 2)])
    run = _Source("run", [_fact("run", "run-1", 1)])
    inspector = TimelineInspector((plan, run))

    async def scenario() -> None:
        first = await inspector.inspect_page({"plan": "p", "run": "r"}, limit=1)
        assert [item.reference for item in first.facts] == ["plan-1"]
        assert first.has_more
        fixed_plan_watermark = first.cursor.watermarks["plan"]
        plan.rows.append(_fact("plan", "plan-later", 3))

        second = await inspector.inspect_page(
            {"plan": "p", "run": "r"}, cursor=first.cursor, limit=1
        )
        third = await inspector.inspect_page(
            {"plan": "p", "run": "r"}, cursor=second.cursor, limit=1
        )
        fourth = await inspector.inspect_page(
            {"plan": "p", "run": "r"}, cursor=third.cursor, limit=1
        )
        refs = [
            item.reference
            for page in (second, third, fourth)
            for item in page.facts
        ]
        assert refs == ["plan-2", "run-1"]
        assert "plan-later" not in refs
        assert fourth.cursor.watermarks["plan"] == fixed_plan_watermark
        assert fourth.has_more is False
        assert all(call[1] == fixed_plan_watermark for call in plan.calls or ())
        assert [call[0] for call in plan.calls or ()] == [None, 1, 2, 2]

    asyncio.run(scenario())


def test_causal_references_order_sources_and_wall_clock_is_display_only() -> None:
    run = _Source(
        "z-run",
        [_fact("z-run", "request-1", 1, causal=("approval-1",), display_at=10.0)],
    )
    plan = _Source("a-plan", [_fact("a-plan", "approval-1", 1, display_at=900.0)])
    report = asyncio.run(
        TimelineInspector((run, plan)).inspect_page(
            {"z-run": "r", "a-plan": "p"}, limit=10
        )
    )
    assert [item.reference for item in report.facts] == ["approval-1", "request-1"]
    assert not any(gap.code is CoverageGapCode.MISSING_RELATIONSHIP for gap in report.gaps)


def test_source_scan_checkpoint_can_cross_unrelated_rows_without_skipping_a_fact() -> None:
    class _FilteredRunSource(_Source):
        async def read_page(
            self,
            identity: object,
            *,
            cursor: str | int | None,
            watermark: str | int,
            limit: int,
        ) -> TimelineSourcePage:
            if cursor is None:
                return TimelineSourcePage(facts=(), next_cursor=5, exhausted=False)
            return await super().read_page(
                identity, cursor=cursor, watermark=watermark, limit=limit
            )

    source = _FilteredRunSource("run", [_fact("run", "run:sequence-7", 7)])

    async def scenario() -> None:
        first = await TimelineInspector((source,)).inspect_page({"run": "run-1"}, limit=10)
        assert first.facts == ()
        assert first.cursor.source_cursors["run"] == 5
        assert first.has_more
        second = await TimelineInspector((source,)).inspect_page(
            {"run": "run-1"}, cursor=first.cursor, limit=10
        )
        assert [item.reference for item in second.facts] == ["run:sequence-7"]

    asyncio.run(scenario())


def test_missing_duplicate_and_unsequenced_sources_are_visible_as_coverage_gaps() -> None:
    left = _Source(
        "left",
        [_fact("left", "duplicate", 1, causal=("absent",))],
        truncate=True,
    )
    right = _Source("right", [_fact("right", "duplicate", 2)])
    snapshot = _Source(
        "snapshot",
        [_fact("snapshot", "operation-a", None, source_cursor="operation-a")],
        sequence_ordered=False,
    )
    missing = _Source("missing", [], fail_watermark=True)
    report = asyncio.run(
        TimelineInspector((left, right, snapshot, missing)).inspect_page(
            {"left": "l", "right": "r", "snapshot": "o", "missing": "m"},
            limit=10,
        )
    )
    codes = {gap.code for gap in report.gaps}
    assert {
        CoverageGapCode.MISSING_RELATIONSHIP,
        CoverageGapCode.DUPLICATE_REFERENCE,
        CoverageGapCode.TRUNCATED,
        CoverageGapCode.SEQUENCE_UNAVAILABLE,
        CoverageGapCode.SOURCE_UNAVAILABLE,
    } <= codes
    assert len([item for item in report.facts if item.reference == "duplicate"]) == 1
    assert any(item.reference == "operation-a" and item.sequence is None for item in report.facts)


def test_rendering_is_bounded_and_sanitized_and_cursor_round_trips() -> None:
    source = _Source("run", [_fact("run", "run-1", 1, summary="Bearer abcdefghijk")])
    report = asyncio.run(
        TimelineInspector((source,)).inspect_page({"run": "r"}, limit=10)
    )
    encoded = timeline_report_to_json(report)
    markdown = timeline_report_to_markdown(report)
    assert "abcdefghijk" not in encoded
    assert "abcdefghijk" not in markdown
    assert json.loads(encoded)["facts"][0]["summary"] == "[redacted]"
    restored = type(report.cursor).from_record(report.cursor.to_record())
    assert restored == report.cursor


def test_timeline_refuses_a_writable_source() -> None:
    source = _Source("run", [], read_only=False)
    with pytest.raises(ConfigurationError, match="not read-only"):
        TimelineInspector((source,))


def test_analysis_counts_outcomes_and_decisions_and_keeps_unknown_durations_unknown() -> None:
    measured_wait = MetricObservation(
        name=MetricName.READY_TO_ADMISSION_WAIT,
        value=2.5,
        unit="seconds",
        clock_domain="monotonic-attempt-1",
        endpoints_same_clock=True,
    )
    unknown_recovery = MetricObservation(
        name=MetricName.RECOVERY_DURATION,
        value=None,
        unit="seconds",
        coverage=MetricCoverage.UNKNOWN,
    )
    source = _Source(
        "plan",
        [
            _fact("plan", "accept-1", 1, kind="acceptance", status="accepted"),
            _fact("plan", "accept-2", 2, kind="acceptance", status="rejected"),
            _fact("plan", "approval-1", 3, kind="approval_decision"),
            _fact(
                "plan",
                "recovery-1",
                4,
                metrics=(measured_wait, unknown_recovery),
                root_cause=RootCauseCategory.STORAGE_CONFLICT,
            ),
        ],
    )
    report = asyncio.run(
        TimelineInspector((source,)).inspect_page({"plan": "p"}, limit=10)
    )
    encoded = json.loads(timeline_report_to_json(report))
    metrics = {item["name"]: item for item in encoded["analysis"]["metrics"]}
    assert metrics["accepted"]["total"] == 1
    assert metrics["rejected"]["total"] == 1
    assert metrics["manual_decision_count"]["total"] == 1
    assert metrics["ready_to_admission_wait"]["mean"] == 2.5
    assert metrics["recovery_duration"]["total"] is None
    assert metrics["recovery_duration"]["coverage"] == "unknown"
    assert encoded["analysis"]["root_causes"] == [
        {
            "category": "storage_conflict",
            "source_id": "plan",
            "reference": "recovery-1",
            "source_kind": "committed",
            "inferred": False,
        }
    ]
    assert "## Metrics" in timeline_report_to_markdown(report)


def test_duration_metric_requires_recorded_same_clock_endpoints() -> None:
    with pytest.raises(ContractError, match="same clock domain"):
        MetricObservation(
            name=MetricName.UNKNOWN_DURATION,
            value=4.0,
            unit="seconds",
            clock_domain="wall-clock",
            endpoints_same_clock=False,
        )


def test_budget_usage_deduplicates_canonical_reservations_and_surfaces_conflicts() -> None:
    plan_usage = MetricObservation(
        name=MetricName.BUDGET_USAGE,
        value=4,
        unit="tokens",
        dedupe_key="reservation:canonical-1",
    )
    duplicate_usage = MetricObservation(
        name=MetricName.BUDGET_USAGE,
        value=4,
        unit="tokens",
        dedupe_key="reservation:canonical-1",
    )
    conflict_usage = MetricObservation(
        name=MetricName.BUDGET_USAGE,
        value=5,
        unit="tokens",
        dedupe_key="reservation:canonical-1",
    )

    async def read(metrics: tuple[MetricObservation, ...]):
        plan = _Source("plan", [_fact("plan", "plan-budget", 1, metrics=(plan_usage,))])
        task = _Source("task", [_fact("task", "task-budget", 1, metrics=metrics)])
        return await TimelineInspector((plan, task)).inspect_page(
            {"plan": "p", "task": "t"}, limit=10
        )

    duplicate_report = asyncio.run(read((duplicate_usage,)))
    duplicate_metric = next(
        item for item in json.loads(timeline_report_to_json(duplicate_report))["analysis"]["metrics"]
        if item["name"] == "budget_usage"
    )
    assert duplicate_metric["total"] == 4
    assert duplicate_metric["duplicate_rows"] == 1

    conflict_report = asyncio.run(read((conflict_usage,)))
    conflict_metric = next(
        item for item in json.loads(timeline_report_to_json(conflict_report))["analysis"]["metrics"]
        if item["name"] == "budget_usage"
    )
    assert conflict_metric["total"] is None
    assert conflict_metric["coverage"] == "partial"
    assert conflict_metric["conflicting_rows"] == 1


def test_plan_inspector_exposes_an_optional_correlated_read_port() -> None:
    source = _Source("run", [_fact("run", "run-1", 1)])
    inspector = TimelineInspector((source,))
    plan_inspector = PlanInspector(control=type("ReadOnlyControl", (), {"read_only": True})(), linked_timeline=inspector)

    async def scenario() -> None:
        result = await plan_inspector.inspect_linked_timeline({"run": "r"}, limit=10)
        assert [item.reference for item in result.facts] == ["run-1"]
        with pytest.raises(ConfigurationError, match="no linked timeline"):
            await PlanInspector(control=type("ReadOnlyControl", (), {"read_only": True})()).inspect_linked_timeline(
                {"run": "r"}
            )

    asyncio.run(scenario())
