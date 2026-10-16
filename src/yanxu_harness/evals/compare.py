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

from .types import (
    LEGACY_EVAL_DIGEST_ALGORITHM,
    SUPPORTED_EVAL_DIGEST_ALGORITHMS,
    EvalCase,
    EvalCaseResult,
    EvalOutcome,
    EvalSuite,
    looks_like_fingerprint,
)

__all__ = ["compare_reports", "COMPARISON_VERSION"]

COMPARISON_VERSION = 1

#: The fields that must agree for two results to answer the same question.
_MATCH_FIELDS = ("case_id", "case_version", "case_digest")


def _case_records(suite: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """The suite's own record of each case it ran, if the report carries one."""
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in suite.get("cases") or ():
        if not isinstance(record, Mapping):
            continue
        out[(str(record.get("case_id") or ""), str(record.get("version") or ""))] = record
    return out


def _identity_problem(
    result: EvalCaseResult,
    records: Mapping[tuple[str, str], Mapping[str, Any]],
    suite: Mapping[str, Any],
) -> str:
    """Why this result may not be compared — or an empty string when it may.

    An identity is only usable when the build knows the algorithm it was written with, the
    digest is shaped like a fingerprint, and (when the report carries its own case records)
    the digest the result states is the digest of the question that suite actually declares.
    Trusting whatever string a JSON file happens to carry under ``case_digest`` is exactly how
    two different questions end up in one comparison row.
    """
    algorithm = result.case_digest_algorithm
    if algorithm not in SUPPORTED_EVAL_DIGEST_ALGORITHMS:
        return (
            f"digest algorithm {algorithm} is not one this build can check; refusing to treat "
            "the result as comparable"
        )
    if algorithm == LEGACY_EVAL_DIGEST_ALGORITHM:
        return (
            "written with digest algorithm 1, which does not cover the acceptance truth or the "
            "fixture's test command; this build cannot establish that the two sides answered "
            "the same question"
        )
    if not result.case_digest:
        return "the result carries no case digest, so the question it answers is unknown"
    if not looks_like_fingerprint(result.case_digest):
        return f"case_digest {result.case_digest!r} is not a fingerprint this build writes"
    record = records.get((result.case_id, result.case_version))
    if record is not None:
        stated = str(record.get("digest") or "")
        if stated != result.case_digest:
            return (
                f"the result states case digest {result.case_digest} but the suite record for "
                f"{result.case_id!r} says {stated or 'nothing'}"
            )
        try:
            recomputed = EvalCase.from_record(record).digest
        except Exception as exc:  # noqa: BLE001 - an unreadable case record is the finding
            return f"the suite record for {result.case_id!r} cannot be re-read: {exc}"
        if recomputed != result.case_digest:
            return (
                "the result's case digest does not match the digest of the case record it "
                f"claims to answer ({recomputed})"
            )
    elif suite.get("cases"):
        return (
            f"the suite lists cases but not {result.case_id!r} v{result.case_version!r}, so the "
            "result's digest cannot be checked against the question it claims to answer"
        )
    return ""


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


def _match_key(result: EvalCaseResult) -> tuple[str, str, int, str]:
    """The identity of the question: which case, which version, which algorithm, which digest."""
    return (
        result.case_id,
        result.case_version,
        result.case_digest_algorithm,
        result.case_digest,
    )


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


def _sample_key(result: EvalCaseResult) -> tuple[str, str, int, str, int]:
    """The frozen key of one sample: the question it answered *and* which repeat it was."""
    return (*_match_key(result), result.repeat_index)


def _unique_samples(results: Sequence[EvalCaseResult]) -> list[EvalCaseResult]:
    """Drop repeated sample keys, keeping the first: a reference is not a second observation.

    A result that appears twice (a report that lists it twice, a caller that concatenates two
    reports) must not add a second elapsed sample, a second usage total or a second weight in
    the pass rate. The duplicates are reported separately, by the caller.
    """
    seen: set[tuple[str, str, int, str, int]] = set()
    out: list[EvalCaseResult] = []
    for result in results:
        key = _sample_key(result)
        if key in seen:
            continue
        seen.add(key)
        out.append(result)
    return out


def _partition(
    results: Sequence[EvalCaseResult],
    records: Mapping[tuple[str, str], Mapping[str, Any]],
    suite: Mapping[str, Any],
    side: str,
    problems: list[Mapping[str, Any]],
) -> tuple[list[EvalCaseResult], int]:
    """Split one side's results into usable samples and stated problems.

    A result is usable when its identity can be established (F2) *and* it is the only result
    with its sample key. Two results claiming the same case, digest and repeat index are not
    two samples: one of them cannot be attributed, so both are listed as invalid rather than
    letting either stand in for the other.
    """
    usable: list[EvalCaseResult] = []
    by_key: dict[tuple[str, str, int, str, int], list[EvalCaseResult]] = {}
    for result in results:
        problem = _identity_problem(result, records, suite)
        if problem:
            problems.append(
                {
                    "case_id": result.case_id,
                    "repeat_index": result.repeat_index,
                    "side": side,
                    "reason": problem,
                    "outcome": result.outcome.value,
                }
            )
            continue
        by_key.setdefault(_sample_key(result), []).append(result)
    duplicate_keys = 0
    for key, group in by_key.items():
        if len(group) == 1:
            usable.append(group[0])
            continue
        duplicate_keys += len(group)
        for result in group:
            problems.append(
                {
                    "case_id": result.case_id,
                    "repeat_index": result.repeat_index,
                    "side": side,
                    "reason": (
                        f"{len(group)} results share one sample key (case {key[0]!r}, digest "
                        f"{key[3]}, repeat {key[4]}); a sample key must identify exactly one run"
                    ),
                    "outcome": result.outcome.value,
                }
            )
    return usable, duplicate_keys


def _per_case_samples(results: Sequence[EvalCaseResult]) -> Mapping[str, Mapping[str, Any]]:
    """How many samples a case really has, per side, counted per case and not in total.

    Five cases with one observation each is five cases with one observation, not five samples
    per case. Only a task verdict (`passed`/`failed`) counts as a valid sample: a run that never
    happened or could not start measures nothing about the model, and is reported by category.
    """
    per_case: dict[str, dict[str, Any]] = {}
    for result in _unique_samples(results):
        bucket = per_case.setdefault(
            result.case_id,
            {"samples": 0, "task_verdicts": 0, "not_run": 0, "cancelled": 0,
             "infra_or_invalid": 0, "outcomes": {}},
        )
        bucket["outcomes"][result.outcome.value] = (
            bucket["outcomes"].get(result.outcome.value, 0) + 1
        )
        if result.outcome.is_task_verdict:
            bucket["samples"] += 1
            bucket["task_verdicts"] += 1
        elif result.outcome is EvalOutcome.NOT_RUN:
            bucket["not_run"] += 1
        elif result.outcome is EvalOutcome.CANCELLED:
            bucket["cancelled"] += 1
        else:
            bucket["infra_or_invalid"] += 1
    return per_case


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
    before_records = _case_records(before_key)
    after_records = _case_records(after_key)

    incomparable: list[Mapping[str, Any]] = []
    usable_before, duplicate_before = _partition(
        before, before_records, before_key, "before", incomparable
    )
    usable_after, duplicate_after = _partition(
        after, after_records, after_key, "after", incomparable
    )

    # One-to-one, by the frozen sample key. There is deliberately no fallback to "some other
    # sample of the same case": a missing repeat is a missing observation, and pairing it with
    # a different run invents a sample, a cost and a weight that never existed.
    before_index: dict[tuple[str, str, int, str, int], EvalCaseResult] = {
        _sample_key(result): result for result in usable_before
    }
    before_questions = {_match_key(result) for result in usable_before}
    after_repeat_counts: dict[tuple[str, str, int, str], set[int]] = {}
    for result in usable_before:
        after_repeat_counts.setdefault(_match_key(result), set()).add(result.repeat_index)

    rows: list[Mapping[str, Any]] = []
    matched_before: list[EvalCaseResult] = []
    matched_after: list[EvalCaseResult] = []
    for result in usable_after:
        partner = before_index.get(_sample_key(result))
        if partner is None:
            if _match_key(result) in before_questions:
                reason = (
                    f"the baseline has {len(after_repeat_counts[_match_key(result)])} sample(s) "
                    f"for this question and none with repeat index {result.repeat_index}, so "
                    "there is no observation to pair this run with"
                )
            else:
                reason = (
                    "the case/fixture/verifier digest does not appear in the baseline, so "
                    "there is nothing comparable to report"
                )
            incomparable.append(
                {
                    "case_id": result.case_id,
                    "repeat_index": result.repeat_index,
                    "side": "after",
                    "reason": reason,
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

    matched_keys = {_sample_key(result) for result in matched_before}
    for result in usable_before:
        if _sample_key(result) not in matched_keys:
            incomparable.append(
                {
                    "case_id": result.case_id,
                    "repeat_index": result.repeat_index,
                    "side": "before",
                    "reason": "the baseline has this sample and the candidate does not",
                    "before": result.outcome.value,
                }
            )

    # How many samples each side actually has, per case. The caveat is about the smallest case,
    # not about the total: five cases with one observation each is not five samples per case.
    per_case_before = _per_case_samples(usable_before)
    per_case_after = _per_case_samples(usable_after)
    case_ids = sorted(set(per_case_before) | set(per_case_after))
    per_case = {
        case_id: {
            "samples_before": per_case_before.get(case_id, {}).get("samples", 0),
            "samples_after": per_case_after.get(case_id, {}).get("samples", 0),
            "not_run_before": per_case_before.get(case_id, {}).get("not_run", 0),
            "not_run_after": per_case_after.get(case_id, {}).get("not_run", 0),
            "infra_or_invalid_before": per_case_before.get(case_id, {}).get(
                "infra_or_invalid", 0
            ),
            "infra_or_invalid_after": per_case_after.get(case_id, {}).get(
                "infra_or_invalid", 0
            ),
        }
        for case_id in case_ids
    }
    smallest = min(
        (value["samples_after"] for value in per_case.values()),
        default=0,
    )

    caveats: list[str] = []
    if not before_records and usable_before:
        caveats.append(
            "the baseline report carries no suite records, so each result's case digest could "
            "not be checked against the question it claims to answer"
        )
    if not after_records and usable_after:
        caveats.append(
            "the candidate report carries no suite records, so each result's case digest could "
            "not be checked against the question it claims to answer"
        )
    if real_model and smallest < 5:
        caveats.append(
            f"at most {smallest} valid sample(s) per case: reported as a distribution, not as a "
            "significance claim"
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
        # The cost a comparison may state is the cost of the *paired* observations, and the
        # distribution a reader needs is each side's own raw samples. Keeping them in separate
        # sections is what stops a missing partner from being read as a measurement.
        "cost": {
            "before": _cost(_unique_samples(matched_before)),
            "after": _cost(_unique_samples(matched_after)),
        },
        "distribution": {
            "before": _cost(_unique_samples(usable_before)),
            "after": _cost(_unique_samples(usable_after)),
        },
        "samples": {
            "before": {
                "listed": len(before),
                "usable": len(usable_before),
                "duplicates": duplicate_before,
                "not_run": sum(
                    1 for item in usable_before if item.outcome is EvalOutcome.NOT_RUN
                ),
                "infra_or_invalid": sum(
                    1
                    for item in usable_before
                    if item.outcome in {EvalOutcome.INFRA_ERROR, EvalOutcome.INVALID_RESULT}
                ),
                "matched": len(matched_before),
            },
            "after": {
                "listed": len(after),
                "usable": len(usable_after),
                "duplicates": duplicate_after,
                "not_run": sum(
                    1 for item in usable_after if item.outcome is EvalOutcome.NOT_RUN
                ),
                "infra_or_invalid": sum(
                    1
                    for item in usable_after
                    if item.outcome in {EvalOutcome.INFRA_ERROR, EvalOutcome.INVALID_RESULT}
                ),
                "matched": len(matched_after),
            },
            "per_case": per_case,
        },
        "caveats": caveats,
    }


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


_ = (EvalSuite,)
