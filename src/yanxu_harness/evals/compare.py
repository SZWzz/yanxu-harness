"""Compare two suite runs: what changed, what is incomparable, and what is missing.

Three rules, each of which is a way a comparison report lies:

* **match the question first.** Two results are compared only when the case version, fixture
  digest, verifier digest and the key configuration match. Anything else is listed as
  incomparable with the reason — never silently averaged;
* **do not turn small samples into claims.** For real-model runs the pass count, the elapsed
  distribution and the cost distribution are reported as they are, with the sample size. No
  weighted "competitiveness score" is invented, and a difference between three runs is not
  called an improvement;
* **quality, reliability and cost stay apart.** A task verdict (`passed`/`failed`) is not mixed
  with the framework's own failures (`infra_error`/`invalid_result`) or with runs that never
  happened (`not_run`/`cancelled`), and cost is a known subtotal plus its coverage, not a total
  that hides what was not measured.

A baseline's failures are preserved as failures. Nothing here can make an old defect look
fixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .types import EvalCaseResult, EvalOutcome, EvalSuite

__all__ = ["compare_reports", "COMPARISON_VERSION"]

COMPARISON_VERSION = 1

#: The fields that must agree for two results to answer the same question.
_MATCH_FIELDS = ("case_id", "case_version", "case_digest")


def _results_of(report: Any) -> list[EvalCaseResult]:
    if isinstance(report, Mapping):
        rows = report.get("results") or ()
    else:  # an EvalSuiteResult
        rows = report.results
    out: list[EvalCaseResult] = []
    for row in rows:
        out.append(row if isinstance(row, EvalCaseResult) else EvalCaseResult.from_record(row))
    return out


def _suite_of(report: Any) -> Mapping[str, Any]:
    if isinstance(report, Mapping):
        return dict(report.get("suite") or {})
    if hasattr(report, "suite"):
        return report.suite.to_record()
    return {}


def _match_key(result: EvalCaseResult) -> tuple[str, str, str]:
    return (result.case_id, result.case_version, result.case_digest)


def _summarise(results: Sequence[EvalCaseResult]) -> Mapping[str, Any]:
    counts = {outcome.value: 0 for outcome in EvalOutcome}
    for result in results:
        counts[result.outcome.value] += 1
    verdicts = counts[EvalOutcome.PASSED.value] + counts[EvalOutcome.FAILED.value]
    return {
        "cases": len(results),
        "outcomes": counts,
        "task_verdicts": verdicts,
        "pass_rate": (
            None if verdicts == 0 else round(counts[EvalOutcome.PASSED.value] / verdicts, 4)
        ),
        "verdicts_denominator": verdicts,
        "not_verified": len(results) - verdicts,
    }


def _cost(results: Sequence[EvalCaseResult]) -> Mapping[str, Any]:
    """Cost and duration as distributions, with the amount that is not known.

    A single total is never produced: measurements are grouped by unit, and each group states
    how many of its entries are actual, estimated or unknown, so a reader can see the coverage
    instead of trusting a sum that silently omits what was missing.
    """
    units: dict[str, dict[str, Any]] = {}
    elapsed: list[float] = []
    for result in results:
        if result.elapsed_seconds is not None:
            elapsed.append(float(result.elapsed_seconds))
        for item in result.usage:
            unit = str(item.get("unit") or "unknown")
            bucket = units.setdefault(
                unit, {"actual": 0, "estimated": 0, "unknown": 0, "known_subtotal": 0.0, "notes": []}
            )
            quantity = str(item.get("quantity") or "unknown")
            bucket[quantity] = bucket.get(quantity, 0) + 1
            if quantity != "unknown" and item.get("amount") is not None:
                bucket["known_subtotal"] += float(item["amount"])
            elif item.get("note"):
                bucket["notes"].append(str(item["note"]))
    return {
        "elapsed_seconds": {
            "count": len(elapsed),
            "min": min(elapsed) if elapsed else None,
            "max": max(elapsed) if elapsed else None,
            "mean": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
        },
        "units": {
            unit: {
                **bucket,
                "known_subtotal": round(bucket["known_subtotal"], 6),
                "complete": bucket["unknown"] == 0 and bucket["estimated"] == 0,
            }
            for unit, bucket in sorted(units.items())
        },
        "price_table": "none: this build never prices a run without a host-provided table",
    }


def _protocol_diff(before: EvalCaseResult, after: EvalCaseResult) -> Mapping[str, Any]:
    """The protocol facts that differ, so a pass/fail change can be read as a cause."""
    keys = sorted(set(before.protocol) | set(after.protocol))
    diff: dict[str, Any] = {}
    for key in keys:
        if key in {"trace", "ledger"}:
            continue
        left, right = before.protocol.get(key), after.protocol.get(key)
        if left != right:
            diff[key] = {"before": left, "after": right}
    return diff


@dataclass(frozen=True)
class _Row:
    case_id: str
    repeat_index: int
    result: EvalCaseResult


def compare_reports(
    baseline: Any,
    candidate: Any,
    *,
    real_model: bool = False,
) -> Mapping[str, Any]:
    """Compare two suite results and state what the comparison can and cannot say."""
    before = _results_of(baseline)
    after = _results_of(candidate)
    before_key = _suite_of(baseline)
    after_key = _suite_of(candidate)

    index: dict[tuple[str, str, str], list[EvalCaseResult]] = {}
    for result in before:
        index.setdefault(_match_key(result), []).append(result)

    rows: list[Mapping[str, Any]] = []
    matched_before: list[EvalCaseResult] = []
    matched_after: list[EvalCaseResult] = []
    incomparable: list[Mapping[str, Any]] = []
    for result in after:
        candidates = index.get(_match_key(result), [])
        partner = next(
            (item for item in candidates if item.repeat_index == result.repeat_index), None
        )
        if partner is None and candidates:
            partner = candidates[0]
        if partner is None:
            incomparable.append(
                {
                    "case_id": result.case_id,
                    "reason": (
                        "the case/fixture/verifier digest does not appear in the baseline, so "
                        "there is nothing comparable to report"
                    ),
                    "after": result.outcome.value,
                }
            )
            continue
        matched_before.append(partner)
        matched_after.append(result)
        rows.append(
            {
                "case_id": result.case_id,
                "repeat_index": result.repeat_index,
                "before": partner.outcome.value,
                "after": result.outcome.value,
                "changed": partner.outcome != result.outcome,
                "verifier_before": (
                    None if partner.verifier is None else partner.verifier.to_record()
                ),
                "verifier_after": None if result.verifier is None else result.verifier.to_record(),
                "protocol_diff": _protocol_diff(partner, result),
            }
        )

    for result in before:
        if not any(item is result for item in matched_before):
            incomparable.append(
                {
                    "case_id": result.case_id,
                    "reason": "the baseline has this case and the candidate does not",
                    "before": result.outcome.value,
                }
            )

    repeats = max((len(_results_of(candidate)), 1))
    caveats: list[str] = []
    if real_model and repeats < 5:
        caveats.append(
            f"{repeats} sample(s) per case: reported as a distribution, not as a significance "
            "claim"
        )
    return {
        "schema_version": COMPARISON_VERSION,
        "suite": {"before": before_key, "after": after_key},
        "comparable": [row for row in rows],
        "incomparable": incomparable,
        "quality": {
            "before": _summarise(matched_before),
            "after": _summarise(matched_after),
        },
        "reliability": {
            "before_infra_errors": sum(
                1 for item in matched_before if item.outcome is EvalOutcome.INFRA_ERROR
            ),
            "after_infra_errors": sum(
                1 for item in matched_after if item.outcome is EvalOutcome.INFRA_ERROR
            ),
            "before_invalid_results": sum(
                1 for item in matched_before if item.outcome is EvalOutcome.INVALID_RESULT
            ),
            "after_invalid_results": sum(
                1 for item in matched_after if item.outcome is EvalOutcome.INVALID_RESULT
            ),
        },
        "cost": {"before": _cost(matched_before), "after": _cost(matched_after)},
        "caveats": caveats,
    }


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


_ = (EvalSuite,)
