"""Render one report: safe JSON as the fact format, Markdown as a derived view of it.

Two rules, and both exist because the alternative leaks something:

* **JSON is the fact format.** It is emitted with a stable field order and no NaN/Infinity
  (which are not JSON), so a host can diff two reports and store one as evidence;
* **Markdown is derived from the same report, never from the stores.** If the two views
  needed separate statistics, one of them would eventually disagree with the other.

Everything a report publishes is either an id, a number, a status or a *phrase this build
wrote*. Free text can only enter through ``detail``/``reason``/``note`` fields, and those
came from a record or a host, so the Markdown view escapes them: a summary that rendered a
tool's detail text verbatim could be made to inject HTML into whatever consumes it.
"""

from __future__ import annotations

import json
from typing import Any

from .types import RunDiagnosticReport, SourceKind, UsageQuantity

__all__ = ["to_json", "to_markdown", "escape_markdown"]

_ESCAPE = {
    "\\": "\\\\",
    "`": "\\`",
    "*": "\\*",
    "_": "\\_",
    "[": "\\[",
    "]": "\\]",
    "<": "&lt;",
    ">": "&gt;",
    "|": "\\|",
    "\n": " ",
    "\r": " ",
}


def escape_markdown(text: Any) -> str:
    """Escape one untrusted value for a Markdown table cell.

    Newlines become spaces (a cell cannot contain a line break), pipes stop splitting the
    row, and angle brackets are entity-escaped so a ``<script>`` in a record cannot become
    markup in a document somebody renders.
    """
    return "".join(_ESCAPE.get(character, character) for character in str(text))


def to_json(report: RunDiagnosticReport) -> str:
    """The report as JSON: sorted keys, UTF-8 text, no NaN."""
    return json.dumps(
        report.to_record(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def to_markdown(report: RunDiagnosticReport) -> str:
    """The same report as Markdown, for a human reading it in a terminal or a PR."""
    lines: list[str] = []
    lines.append(f"# Run `{escape_markdown(report.run_id)}`")
    lines.append("")
    lines.append(
        f"- identity: `{escape_markdown(report.namespace)}` / "
        f"`{escape_markdown(report.session_id)}`"
    )
    lines.append(
        f"- status: **{escape_markdown(report.status or 'unknown')}**"
        + (f" (stop `{escape_markdown(report.stop_code)}`)" if report.stop_code else "")
        + (f" error `{escape_markdown(report.error_code)}`" if report.error_code else "")
    )
    if report.observer_attempt:
        lines.append(f"- read by attempt: `{escape_markdown(report.observer_attempt)}`")
    if report.origin_attempts:
        readable = ", ".join(f"`{escape_markdown(item)}`" for item in report.origin_attempts)
        lines.append(f"- attempts that did the work: {readable}")
    if report.detail:
        lines.append(f"- detail: {escape_markdown(report.detail)}")
    if report.waiting:
        waiting = report.waiting
        lines.append(
            "- waiting for: "
            f"`{escape_markdown(waiting.get('kind'))}` on "
            f"`{escape_markdown(waiting.get('call_id'))}`"
        )
    boundary = report.boundary
    lines.append(
        f"- read boundary: cursor `{boundary.cursor}` → `{boundary.next_cursor}` "
        f"(high water `{boundary.high_water}`), run revision `{boundary.run_revision}`, "
        f"{boundary.scanned_pages} page(s)"
    )
    if boundary.truncated or boundary.scan_limit_hit or boundary.non_atomic:
        lines.append(
            "- boundary caveats: "
            + ", ".join(
                item
                for item in (
                    "truncated" if boundary.truncated else "",
                    "scan limit hit" if boundary.scan_limit_hit else "",
                    "non-atomic view" if boundary.non_atomic else "",
                )
                if item
            )
        )
    lines.append("")

    if report.calls:
        lines.append("## Tool calls")
        lines.append("")
        lines.append("| call | tool | outcome | progress | operation | receipt |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for call in report.calls:
            receipt = call.receipt
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(item)
                    for item in (
                        call.call_id,
                        call.tool_name,
                        call.outcome,
                        call.progress,
                        call.operation_id,
                        f"{receipt.source.value}: {receipt.detail}" if receipt else "none",
                    )
                )
                + " |"
            )
        lines.append("")

    if report.spans:
        lines.append("## Stages")
        lines.append("")
        lines.append("| stage | attempt | started | duration | note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for span in report.spans:
            if span.duration_ms is not None:
                duration = f"{span.duration_ms:.1f} ms"
            elif span.absent:
                duration = "missing"
            elif span.incomplete:
                duration = "incomplete"
            else:
                duration = "not measured"
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(item)
                    for item in (
                        span.stage,
                        span.attempt or "unknown",
                        "yes" if span.started is not None else "missing",
                        duration,
                        span.note,
                    )
                )
                + " |"
            )
        lines.append("")

    if report.usage:
        lines.append("## Measurement")
        lines.append("")
        lines.append("| unit | kind | amount | source | reference | note |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in report.usage:
            amount = "unknown" if item.quantity is UsageQuantity.UNKNOWN else f"{item.amount:g}"
            if item.quantity is UsageQuantity.ESTIMATED:
                amount += " (estimated)"
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(value)
                    for value in (
                        item.unit,
                        item.quantity.value,
                        amount,
                        item.source.value,
                        item.reference,
                        item.note,
                    )
                )
                + " |"
            )
        lines.append("")
        for unit in sorted({item.unit for item in report.usage}):
            coverage = report.usage_coverage(unit=unit)
            if coverage["complete"]:
                continue
            lines.append(
                f"- `{escape_markdown(unit)}`: known subtotal "
                f"{coverage['known_subtotal']:g}, {coverage['unknown']} unknown, "
                f"{coverage['estimated']} estimated — **not a complete total**"
            )
        lines.append("")

    if report.trace:
        lines.append("## Timeline")
        lines.append("")
        lines.append("| reference | kind | attempt | durable | at |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in report.trace:
            lines.append(
                "| "
                + " | ".join(
                    escape_markdown(value)
                    for value in (
                        entry.reference,
                        entry.kind,
                        entry.attempt or entry.attempt_source,
                        "yes" if entry.durable else "no",
                        "" if entry.at is None else f"{entry.at:.3f}",
                    )
                )
                + " |"
            )
        lines.append("")

    if report.integrity:
        lines.append("## What this report cannot prove")
        lines.append("")
        for note in report.integrity:
            lines.append(f"- {escape_markdown(note)}")
        lines.append("")
    if report.unsupported:
        lines.append("## Unsupported here")
        lines.append("")
        for note in report.unsupported:
            lines.append(f"- {escape_markdown(note)}")
        lines.append("")

    counts = report.source_counts()
    lines.append("## Sources")
    lines.append("")
    lines.append(
        ", ".join(
            f"{escape_markdown(kind)}={counts.get(kind, 0)}"
            for kind in sorted({item.value for item in SourceKind})
        )
    )
    lines.append("")
    return "\n".join(lines)
